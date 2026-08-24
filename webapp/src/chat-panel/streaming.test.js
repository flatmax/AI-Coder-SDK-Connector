// Tests for streaming flow: block-keyed chunks, what a completed turn freezes
// onto its message, request-ID filtering, the turn-outcome helper the LED row
// reads, subagent rows, and stream-start error handling.

import { afterEach, describe, expect, it, vi } from 'vitest';

import { resumeActiveStreams } from './events.js';
import { computeTurnOutcome, onStreamRetry } from './streaming.js';
import {
  mountPanel,
  publishFakeRpc,
  pushEvent,
  settle,
} from './test-helpers.js';

// ---------------------------------------------------------------------------
// Streaming via server-push events
// ---------------------------------------------------------------------------

describe('ChatPanel streaming events', () => {
  async function sendAndGetRequestId(panel, message = 'hi') {
    // Send and return the ID the chat panel generated.
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    await settle(panel);
    panel._input = message;
    await panel._send();
    return started.mock.calls[0][0];
  }

  function textChunk(reqId, content, seq = 0) {
    return {
      requestId: reqId,
      chunk: {
        block_id: `${reqId}:b0`,
        seq,
        content,
        done: false,
      },
    };
  }

  it('renders streaming chunks in the assistant slot', async () => {
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p, 'hi');
    pushEvent('stream-chunk', textChunk(reqId, 'Hello', 0));
    pushEvent('stream-chunk', textChunk(reqId, 'Hello, world', 1));
    await settle(p);
    const streaming = p.shadowRoot.querySelector(
      '.message-card.streaming',
    );
    expect(streaming).toBeTruthy();
    expect(streaming.textContent).toContain('Hello, world');
  });

  it('ignores chunks for other request IDs', async () => {
    // Collaboration — a stream from another user's prompt
    // arrives with a different request ID.
    const p = mountPanel();
    await sendAndGetRequestId(p, 'hi');
    pushEvent(
      'stream-chunk',
      textChunk('other-request-id', 'should not render', 0),
    );
    await settle(p);
    const streaming = p.shadowRoot.querySelector(
      '.message-card.streaming',
    );
    expect(streaming).toBeTruthy();
    expect(streaming.textContent).not.toContain('should not');
    // Still waiting on its own first chunk, rather than showing someone
    // else's turn.
    expect(streaming.querySelector('.turn-waiting')).toBeTruthy();
  });

  it('moves streamed content into messages on stream-complete', async () => {
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p, 'hi');
    pushEvent('stream-chunk', textChunk(reqId, 'partial', 0));
    pushEvent('stream-complete', {
      requestId: reqId,
      result: { response: 'final answer', terminal_reason: 'completed' },
    });
    await settle(p);
    expect(p.messages).toHaveLength(2);
    expect(p.messages[1].role).toBe('assistant');
    expect(p.messages[1].content).toBe('final answer');
    expect(p._streaming).toBe(false);
    expect(p._streamingContent).toBe('');
    expect(
      p.shadowRoot.querySelector('.message-card.streaming'),
    ).toBeNull();
  });

  it('a stopped turn keeps what it had produced', async () => {
    // The panel no longer reconstructs the partial text from its own
    // accumulator. The engine's `response` is the concatenation of the
    // turn's text blocks, and an interrupted turn reports what it got to.
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p, 'hi');
    pushEvent('stream-chunk', textChunk(reqId, 'partial content', 0));
    pushEvent('stream-complete', {
      requestId: reqId,
      result: {
        response: 'partial content',
        cancelled: true,
        terminal_reason: 'aborted_streaming',
      },
    });
    await settle(p);
    expect(p.messages[1].content).toBe('partial content');
    expect(p.messages[1].blocks[0].content).toBe('partial content');
    // An interruption badges next to the role label, not as an error.
    const badge = p.shadowRoot.querySelector('.finish-reason-badge');
    expect(badge).toBeTruthy();
    expect(badge.className).toContain('severity-neutral');
  });

  it('an errored completion toasts the engine reason', async () => {
    // There is no `result.error`. A failed turn reports `is_error` with the
    // CLI's own `errors` list, and the panel says what the engine said
    // rather than classifying it — the CLI owns the retry and the taxonomy.
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p, 'hi');
    const toastListener = vi.fn();
    window.addEventListener('aic-toast', toastListener);
    try {
      pushEvent('stream-complete', {
        requestId: reqId,
        result: {
          response: '',
          is_error: true,
          terminal_reason: 'engine_error',
          errors: ['something broke'],
        },
      });
      await settle(p);
      const errors = toastListener.mock.calls
        .map((c) => c[0].detail)
        .filter((d) => d.type === 'error');
      expect(errors.length).toBeGreaterThan(0);
      expect(errors[0].message).toContain('something broke');
      expect(p.messages[1].role).toBe('assistant');
      expect(p.messages[1].turn.is_error).toBe(true);
    } finally {
      window.removeEventListener('aic-toast', toastListener);
    }
  });
});

// ---------------------------------------------------------------------------
// Run timer — how long the assistant has been running
// ---------------------------------------------------------------------------

describe('ChatPanel run timer', () => {
  // Drive Date.now() deterministically so elapsed/frozen
  // durations are exact rather than wall-clock dependent.
  afterEach(() => {
    vi.restoreAllMocks();
  });

  async function sendAt(panel, nowMs, message = 'hi') {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    await settle(panel);
    vi.spyOn(Date, 'now').mockReturnValue(nowMs);
    panel._input = message;
    await panel._send();
    return started.mock.calls[0][0];
  }

  it('stamps the start time and shows a live timer while streaming', async () => {
    const p = mountPanel();
    const reqId = await sendAt(p, 1_000_000);
    // Advance the clock; the streaming card recomputes
    // elapsed from Date.now() - streamStartedAt on render.
    Date.now.mockReturnValue(1_004_200);
    pushEvent('stream-chunk', {
      requestId: reqId,
      chunk: {
        block_id: `${reqId}:b0`,
        seq: 0,
        content: 'Hi',
        done: false,
      },
    });
    await settle(p);
    expect(p._streamStartedAt).toBe(1_000_000);
    const streaming = p.shadowRoot.querySelector(
      '.message-card.streaming',
    );
    const timer = streaming.querySelector('.run-timer-live');
    expect(timer).toBeTruthy();
    expect(timer.textContent).toContain('4.2s');
  });

  it('freezes the duration onto the settled assistant message', async () => {
    const p = mountPanel();
    const reqId = await sendAt(p, 2_000_000);
    Date.now.mockReturnValue(2_007_500);
    pushEvent('stream-complete', {
      requestId: reqId,
      result: { response: 'done' },
    });
    await settle(p);
    expect(p.messages[1].durationMs).toBe(7500);
    // Timer is cleared once the stream settles.
    expect(p._streamStartedAt).toBeNull();
    // The frozen badge renders on the settled card and the
    // live streaming card is gone.
    expect(
      p.shadowRoot.querySelector('.message-card.streaming'),
    ).toBeNull();
    const settled = p.shadowRoot.querySelectorAll(
      '.message-card.role-assistant',
    );
    const badge = settled[settled.length - 1].querySelector(
      '.run-timer',
    );
    expect(badge).toBeTruthy();
    expect(badge.textContent).toContain('7.5s');
  });

  it('records duration on errored completions too', async () => {
    const p = mountPanel();
    const reqId = await sendAt(p, 3_000_000);
    Date.now.mockReturnValue(3_002_000);
    pushEvent('stream-complete', {
      requestId: reqId,
      result: {
        response: '',
        is_error: true,
        terminal_reason: 'engine_error',
        errors: ['boom'],
      },
    });
    await settle(p);
    expect(p.messages[1].durationMs).toBe(2000);
  });

  it('stops the ticker once the stream completes', async () => {
    const p = mountPanel();
    const reqId = await sendAt(p, 4_000_000);
    expect(p._streamTimerInterval).not.toBeNull();
    expect(p._streamTimerInterval).toBeDefined();
    pushEvent('stream-complete', {
      requestId: reqId,
      result: { response: 'ok' },
    });
    await settle(p);
    expect(p._streamTimerInterval == null).toBe(true);
  });

  it('omits the duration when no start stamp exists', async () => {
    // A completion for a request the tab never armed (no
    // matching send) — e.g. an adopted collaborator stream.
    // streamStartedAt is null, so no durationMs is recorded.
    const p = mountPanel();
    await settle(p);
    pushEvent('stream-complete', {
      requestId: 'never-sent',
      result: { response: 'orphan' },
    });
    await settle(p);
    // The orphan completion doesn't match the main tab's
    // currentRequestId, so it produces no settled message.
    expect(
      p.messages.some((m) => m.durationMs !== undefined),
    ).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// What onStreamComplete freezes onto the settled message
// ---------------------------------------------------------------------------
//
// This section used to pin `turn_id` and `agent_blocks` threading for the
// agent-browser affordance. Both are gone (see the agent-tabs section). What a
// settled message carries now is the turn itself: its blocks, its subagent
// rows, the files it touched, and the result payload the footer renders.
//
// "Frozen" is the load-bearing word. The live block records are mutated in
// place while a turn runs, and the same state object is reused by the next
// turn, so anything the message keeps has to be a copy — otherwise the next
// turn rewrites history the user has already read.

describe('ChatPanel onStreamComplete freezes the turn', () => {
  async function sendAndGetRequestId(panel, message = 'hi') {
    const started = vi
      .fn()
      .mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    await settle(panel);
    panel._input = message;
    await panel._send();
    return started.mock.calls[0][0];
  }

  it('the blocks survive the turn state being reset', async () => {
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('stream-chunk', {
      requestId: reqId,
      chunk: {
        block_id: `${reqId}:b0`,
        seq: 0,
        content: 'the answer',
        done: true,
      },
    });
    pushEvent('stream-complete', {
      requestId: reqId,
      result: { response: 'the answer', terminal_reason: 'completed' },
    });
    await settle(p);
    // Live state is empty and ready for the next turn...
    expect(p._turnBlocks.blocks).toEqual([]);
    // ...while the message kept its own copy.
    const msg = p.messages[1];
    expect(msg.blocks).toHaveLength(1);
    expect(msg.blocks[0].content).toBe('the answer');
    expect(msg.content).toBe('the answer');
  });

  it('a tool card is copied one level deeper', async () => {
    // A late tool result, or the next turn reusing the record, must not be
    // able to reach into a settled card.
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('tool-use', {
      requestId: reqId,
      data: {
        tool_use_id: 'toolu_1',
        name: 'Read',
        input: { file_path: 'a.py' },
      },
    });
    pushEvent('tool-result', {
      requestId: reqId,
      data: { tool_use_id: 'toolu_1', status: 'ok', preview: 'x = 1' },
    });
    await settle(p);
    const live = p._turnBlocks.index.get('toolu_1');
    expect(live).toBeTruthy();
    pushEvent('stream-complete', {
      requestId: reqId,
      result: { response: '', terminal_reason: 'completed' },
    });
    await settle(p);
    const frozen = p.messages[1].blocks[0];
    expect(frozen).not.toBe(live);
    expect(frozen.tool.name).toBe('Read');
    expect(frozen.result.status).toBe('ok');
  });

  it('the subagent rows ride along', async () => {
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('subagent-event', {
      requestId: reqId,
      data: {
        task_id: 'task-1',
        agent_id: 'agent-1',
        description: 'audit the parser',
        status: 'completed',
        terminal: true,
      },
    });
    pushEvent('stream-complete', {
      requestId: reqId,
      result: { response: 'done', terminal_reason: 'completed' },
    });
    await settle(p);
    const rows = p.messages[1].subagents;
    expect(rows).toHaveLength(1);
    expect(rows[0].description).toBe('audit the parser');
    expect(rows[0].terminal).toBe(true);
    expect(p._turnBlocks.subagents.size).toBe(0);
  });

  it('files are the union of the result and the tool results', async () => {
    // The result message is authoritative when it arrives, but a turn that
    // ended badly may carry tool results it never summarised.
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('tool-use', {
      requestId: reqId,
      data: {
        tool_use_id: 'toolu_1',
        name: 'Write',
        input: { file_path: 'b.py' },
      },
    });
    pushEvent('tool-result', {
      requestId: reqId,
      data: {
        tool_use_id: 'toolu_1',
        status: 'ok',
        files_modified: ['b.py'],
      },
    });
    pushEvent('stream-complete', {
      requestId: reqId,
      result: {
        response: 'wrote them',
        terminal_reason: 'completed',
        files_modified: ['a.py', 'b.py'],
      },
    });
    await settle(p);
    // Deduplicated, the result's own list first.
    expect(p.messages[1].files).toEqual(['a.py', 'b.py']);
  });

  it('the result payload is snapshotted for the footer', async () => {
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    const result = {
      response: 'done',
      terminal_reason: 'completed',
      tool_calls: 3,
      permission_prompts: 1,
      num_turns: 2,
      duration_ms: 4200,
      total_cost_usd: null,
    };
    pushEvent('stream-complete', { requestId: reqId, result });
    await settle(p);
    const msg = p.messages[1];
    expect(msg.turn).toEqual(result);
    expect(msg.turn).not.toBe(result);
    expect(msg.terminalReason).toBe('completed');
  });

  it('terminalReason is null when the engine reports none', async () => {
    // Older CLIs and locally-handled results report nothing, and the badge
    // must then be absent rather than claiming success.
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('stream-complete', {
      requestId: reqId,
      result: { response: 'done' },
    });
    await settle(p);
    expect(p.messages[1].terminalReason).toBeNull();
    expect(
      p.shadowRoot.querySelector('.finish-reason-badge'),
    ).toBeNull();
  });

  it('a bad terminal reason badges the card', async () => {
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('stream-complete', {
      requestId: reqId,
      result: { response: 'gave up', terminal_reason: 'max_turns' },
    });
    await settle(p);
    const badge = p.shadowRoot.querySelector('.finish-reason-badge');
    expect(badge).toBeTruthy();
    expect(badge.getAttribute('title')).toContain('max_turns');
  });

  it('user_message_id lands on the user message, not the reply', async () => {
    // It identifies the turn for `rewind_files`, and "put the files back the
    // way they were before I asked this" belongs on the card that asked.
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p, 'change it');
    pushEvent('stream-complete', {
      requestId: reqId,
      result: {
        response: 'changed',
        terminal_reason: 'completed',
        user_message_id: 'msg-uuid-1',
      },
    });
    await settle(p);
    expect(p.messages[0].role).toBe('user');
    expect(p.messages[0].user_message_id).toBe('msg-uuid-1');
    expect(p.messages[1].user_message_id).toBeUndefined();
  });

  it('a turn that produced nothing appends nothing', async () => {
    // No blocks, no response, no error — a locally-handled command, say. An
    // empty assistant card would read as the agent ignoring the prompt.
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('stream-complete', {
      requestId: reqId,
      result: { response: '', terminal_reason: 'completed' },
    });
    await settle(p);
    expect(p.messages).toHaveLength(1);
    expect(p.messages[0].role).toBe('user');
  });

  it('an empty turn that errored still appends', async () => {
    // A badge with nothing under it is still the honest answer; silence
    // would leave the user waiting for a reply that never comes.
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('stream-complete', {
      requestId: reqId,
      result: {
        response: '',
        is_error: true,
        terminal_reason: 'engine_error',
        errors: ['the CLI exited'],
      },
    });
    await settle(p);
    expect(p.messages).toHaveLength(2);
    expect(p.messages[1].role).toBe('assistant');
    expect(p.messages[1].terminalReason).toBe('engine_error');
  });
});

// ---------------------------------------------------------------------------
// Chunk coalescing and block identity
// ---------------------------------------------------------------------------
//
// The old contract was that every chunk carried the whole accumulated turn, so
// a dropped chunk cost nothing and any one chunk could rebuild the view. The
// replacement is narrower: content is cumulative *within a block*, so a chunk
// can rebuild its own block and nothing else. `seq` is what makes that safe
// against reordering.

describe('ChatPanel chunk coalescing', () => {
  async function startStream(panel) {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    await settle(panel);
    panel._input = 'hi';
    await panel._send();
    return started.mock.calls[0][0];
  }

  function textChunk(reqId, content, seq, block = 'b0') {
    return {
      requestId: reqId,
      chunk: {
        block_id: `${reqId}:${block}`,
        seq,
        content,
        done: false,
      },
    };
  }

  it('the newest chunk for a block is what renders', async () => {
    const p = mountPanel();
    const reqId = await startStream(p);
    pushEvent('stream-chunk', textChunk(reqId, 'a', 0));
    pushEvent('stream-chunk', textChunk(reqId, 'ab', 1));
    pushEvent('stream-chunk', textChunk(reqId, 'abc', 2));
    await settle(p);
    const blocks = p._turnBlocks.blocks;
    expect(blocks).toHaveLength(1);
    expect(blocks[0].content).toBe('abc');
    expect(blocks[0].seq).toBe(2);
  });

  it('a chunk that arrives out of order is discarded', async () => {
    // Not merged, not appended — dropped. The higher `seq` already carries
    // everything the older one did.
    const p = mountPanel();
    const reqId = await startStream(p);
    pushEvent('stream-chunk', textChunk(reqId, 'first second', 1));
    pushEvent('stream-chunk', textChunk(reqId, 'first', 0));
    await settle(p);
    expect(p._turnBlocks.blocks[0].content).toBe('first second');
  });

  it('a second block appends rather than replacing', async () => {
    // Block identity is the whole point: text either side of a tool call is
    // two blocks, and the later one must not overwrite the earlier.
    const p = mountPanel();
    const reqId = await startStream(p);
    pushEvent('stream-chunk', textChunk(reqId, 'before the call', 0, 'b0'));
    pushEvent('stream-chunk', textChunk(reqId, 'after the call', 0, 'b1'));
    await settle(p);
    expect(p._turnBlocks.blocks.map((b) => b.content)).toEqual([
      'before the call',
      'after the call',
    ]);
    const rendered = p.shadowRoot.querySelectorAll(
      '.message-card.streaming .block-text',
    );
    expect(rendered).toHaveLength(2);
  });

  it('a payload with no block_id is unroutable and ignored', async () => {
    // The engine always sends one. Inventing a block for a payload without
    // it would put unattributable text in the transcript.
    const p = mountPanel();
    const reqId = await startStream(p);
    pushEvent('stream-chunk', {
      requestId: reqId,
      chunk: { seq: 0, content: 'nowhere to put this' },
    });
    await settle(p);
    expect(p._turnBlocks.blocks).toEqual([]);
  });

  it('_streamingContent is no longer the accumulator', async () => {
    // It survives in tab state as the field the old single-string contract
    // wrote to. Nothing writes it during a turn now; the blocks hold the
    // text. Pinned so a partial revert to string accumulation is loud.
    const p = mountPanel();
    const reqId = await startStream(p);
    pushEvent('stream-chunk', textChunk(reqId, 'abc', 0));
    await settle(p);
    expect(p._streamingContent).toBe('');
  });
});

// ---------------------------------------------------------------------------
// Cancel
// ---------------------------------------------------------------------------

describe('ChatPanel cancel', () => {
  it('calls cancel_streaming with the active request ID', async () => {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    const cancel = vi.fn().mockResolvedValue({ status: 'ok' });
    publishFakeRpc({
      'ClaudeCodeService.chat_streaming': started,
      'ClaudeCodeService.cancel_streaming': cancel,
    });
    const p = mountPanel();
    await settle(p);
    p._input = 'hi';
    await p._send();
    await settle(p);
    p.shadowRoot.querySelector('.send-button.stop').click();
    await settle(p);
    expect(cancel).toHaveBeenCalledOnce();
    expect(cancel.mock.calls[0][0]).toBe(started.mock.calls[0][0]);
  });

  it('recovers locally when cancel fails', async () => {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    const cancel = vi
      .fn()
      .mockRejectedValue(new Error('already done'));
    publishFakeRpc({
      'ClaudeCodeService.chat_streaming': started,
      'ClaudeCodeService.cancel_streaming': cancel,
    });
    const p = mountPanel();
    await settle(p);
    const consoleSpy = vi
      .spyOn(console, 'warn')
      .mockImplementation(() => {});
    try {
      p._input = 'hi';
      await p._send();
      await p._cancel();
      await settle(p);
      expect(p._streaming).toBe(false);
    } finally {
      consoleSpy.mockRestore();
    }
  });
});

// ---------------------------------------------------------------------------
// Retry prompt population — removed
// ---------------------------------------------------------------------------
//
// Twelve tests used to check that a failed edit populated the textarea with a
// follow-up prompt: "retry with more surrounding context", "the file has been
// added to the context", and so on. They read `result.edit_results`, which was
// our apply pipeline's report on the marker blocks it had just tried to apply.
//
// The agent applies its own edits. A failed `Edit` tool call comes back to the
// agent as a tool result and it retries on its own — usually before the user
// has finished reading the card. Writing a retry prompt into the box would put
// words in the user's mouth about a failure that has already been handled, so
// the builders went with the pipeline.

// ---------------------------------------------------------------------------
// Agent tabs and subagents
// ---------------------------------------------------------------------------
//
// Twenty-two tests used to live here, driving `stream-complete` payloads with
// `{turn_id, agent_blocks}` and asserting a tab appeared per block. The native
// engine spawned real sibling conversations that way, each with its own
// `ContextManager`, and each needed a tab to talk to.
//
// Claude Code's subagents are internal to a turn: the agent calls `Task`, the
// subagent's output is attributed to that call's `tool_use_id`, and there is
// nothing to send a message to. So they render as a row nested under the card
// that spawned them, and `streamComplete` spawns nothing.
//
// `spawnAgentTabs` and the `agentsSpawned` broadcast that drives it are still
// wired — they go in phase 3 with the rest of the native engine, so that the
// phase-2 diff is about the new path rather than about removing the old one.
// Nothing emits `agentsSpawned` on the Claude Code path, which is why the
// first test below matters: the removal has to be provable from the panel's
// behaviour, not from the absence of a caller.

describe('ChatPanel agent tabs', () => {
  async function startMainStream(panel, message = 'hi') {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    await settle(panel);
    panel._input = message;
    await panel._send();
    await settle(panel);
    return started.mock.calls[0][0];
  }

  it('a completion carrying agent_blocks spawns nothing', async () => {
    const p = mountPanel();
    const reqId = await startMainStream(p);
    pushEvent('stream-complete', {
      requestId: reqId,
      result: {
        response: 'delegated',
        terminal_reason: 'completed',
        turn_id: 'turn_abc',
        agent_blocks: [
          { id: 'frontend-trivial', task: 'first', agent_idx: 0 },
          { id: 'backend-auth', task: 'second', agent_idx: 1 },
        ],
      },
    });
    await settle(p);
    expect(p._tabs.size).toBe(1);
    expect(p._tabs.has('main')).toBe(true);
    // Nor is either field lifted onto the settled message — the
    // "View agents (N)" affordance they fed is gone with them.
    expect(p.messages[1].agent_blocks).toBeUndefined();
    expect(p.messages[1].turn_id).toBeUndefined();
  });

  it('the agentsSpawned broadcast still spawns tabs', async () => {
    const p = mountPanel();
    const reqId = await startMainStream(p);
    pushEvent('agents-spawned', {
      turn_id: 'turn_abc',
      parent_request_id: reqId,
      agent_blocks: [
        {
          id: 'auth-refactor',
          task: 'refactor the auth module',
          agent_idx: 0,
        },
        { id: 'a1', task: '', agent_idx: 1 },
      ],
    });
    await settle(p);
    expect(p._tabs.size).toBe(3);
    // Keyed by the block id verbatim, labelled by index and task.
    expect(p._tabLabels.get('auth-refactor')).toBe(
      'Agent 00: refactor the auth module',
    );
    expect(p._tabLabels.get('a1')).toBe('Agent 01');
    const tab = p._tabs.get('auth-refactor');
    // Seeded with the task, so the tab opens showing what it was asked.
    expect(tab.messages).toEqual([
      { role: 'user', content: 'refactor the auth module' },
    ]);
    // A spawned tab used to inherit a COPY of main's selected-files
    // list, so that an agent tab ticking its own boxes didn't move the
    // main tab's. Tab state carries no file list at all now (CC-21).
    // Focus stays where the user left it.
    expect(p._activeTabId).toBe('main');
  });

  it('a malformed agentsSpawned payload spawns nothing', async () => {
    const p = mountPanel();
    const reqId = await startMainStream(p);
    const block = { id: 'a', task: 't', agent_idx: 0 };
    const malformed = [
      { parent_request_id: reqId, agent_blocks: [block] },
      { turn_id: '', parent_request_id: reqId, agent_blocks: [block] },
      { turn_id: 't', parent_request_id: reqId, agent_blocks: [] },
      { turn_id: 't', parent_request_id: reqId, agent_blocks: 'nope' },
      { turn_id: 't', parent_request_id: reqId },
      {
        turn_id: 't',
        parent_request_id: reqId,
        agent_blocks: [{ id: 'a', task: 't', agent_idx: 'zero' }],
      },
      {
        turn_id: 't',
        parent_request_id: reqId,
        agent_blocks: [null, undefined],
      },
    ];
    for (const detail of malformed) {
      pushEvent('agents-spawned', detail);
    }
    await settle(p);
    expect(p._tabs.size).toBe(1);
  });

  it('a subagent renders as a row inside the turn', async () => {
    // The replacement for a tab: nested under the `Task` card that spawned
    // it, live until its status is terminal, with Stop as the only write
    // affordance — AIC⚡DC did not create it and cannot message it.
    const p = mountPanel();
    const reqId = await startMainStream(p);
    pushEvent('tool-use', {
      requestId: reqId,
      data: {
        tool_use_id: 'toolu_task',
        name: 'Task',
        input: { description: 'audit the parser' },
      },
    });
    pushEvent('subagent-event', {
      requestId: reqId,
      data: {
        task_id: 'task-1',
        agent_id: 'agent-1',
        tool_use_id: 'toolu_task',
        description: 'audit the parser',
        task_type: 'Explore',
        status: 'running',
      },
    });
    await settle(p);
    const row = p.shadowRoot.querySelector('.subagent-row');
    expect(row).toBeTruthy();
    expect(row.classList.contains('live')).toBe(true);
    expect(row.querySelector('.subagent-desc').textContent)
      .toContain('audit the parser');
    expect(row.querySelector('.subagent-stop')).toBeTruthy();
    // A terminal status ends the live state and takes Stop away.
    pushEvent('subagent-event', {
      requestId: reqId,
      data: {
        task_id: 'task-1',
        agent_id: 'agent-1',
        status: 'completed',
        terminal: true,
        summary: 'no issues found',
      },
    });
    await settle(p);
    const done = p.shadowRoot.querySelector('.subagent-row');
    expect(done.classList.contains('terminal')).toBe(true);
    expect(done.querySelector('.subagent-stop')).toBeNull();
    expect(done.querySelector('.subagent-summary').textContent)
      .toContain('no issues found');
  });

  // -------------------------------------------------------------------
  // Background subagents outliving their turn
  // -------------------------------------------------------------------
  //
  // A result message ends a turn, not the run. A subagent spawned with
  // `run_in_background` keeps going past it, the engine keeps following it
  // (`session.py` § `_drain_background`), and the result names what is still
  // going in `background_tasks`.
  //
  // Reading that list as "nothing is running" produced the pair of symptoms
  // this suite pins: an amber "status unknown" LED on a subagent that went on
  // to succeed, and an empty feed behind it — the second because tearing the
  // turn down clears `currentRequestId`, and `findTabForRequest` matches on
  // that alone, so every later block for the turn was unroutable.

  async function backgroundTurn(panel) {
    const reqId = await startMainStream(panel);
    pushEvent('subagent-event', {
      requestId: reqId,
      data: {
        task_id: 'task-1',
        agent_id: 'agent-1',
        tool_use_id: 'toolu_task',
        description: 'describe git log --stat',
        task_type: 'local_agent',
        status: 'running',
      },
    });
    await settle(panel);
    pushEvent('stream-complete', {
      requestId: reqId,
      result: {
        response: 'dispatched',
        terminal_reason: 'completed',
        background_tasks: ['task-1'],
      },
    });
    await settle(panel);
    return reqId;
  }

  function subagentTab(panel) {
    for (const tab of panel._tabs.values()) {
      if (tab.subagent) return tab;
    }
    return null;
  }

  it('a still-running background subagent is not settled by the turn', async () => {
    const p = mountPanel();
    await backgroundTurn(p);
    const tab = subagentTab(p);
    expect(tab).toBeTruthy();
    expect(tab.subagent.settled).toBe(false);
    // "unknown" is the amber LED. It is a claim about a subagent whose
    // terminal event never arrived, and saying it about one that is simply
    // still working is a lie the user then watches get contradicted.
    expect(tab.subagent.unknown).toBe(false);
    expect(tab.streaming).toBe(true);
  });

  it('the turn keeps the routing state its background work arrives through', async () => {
    const p = mountPanel();
    const reqId = await backgroundTurn(p);
    // Main's own presentation is finished — footer rendered, composer free.
    expect(p._streaming).toBe(false);
    expect(p.messages.at(-1).content).toBe('dispatched');
    // But the turn is still addressable, which is what makes the events
    // below land anywhere at all.
    expect(p._tabs.get('main').currentRequestId).toBe(reqId);
  });

  it('a background subagent’s later blocks reach its feed', async () => {
    const p = mountPanel();
    const reqId = await backgroundTurn(p);
    pushEvent('tool-use', {
      requestId: reqId,
      data: {
        tool_use_id: 'toolu_bash',
        name: 'Bash',
        input: { command: 'git log --stat' },
        agent_id: 'toolu_task',
      },
    });
    await settle(p);
    const tab = subagentTab(p);
    const commands = tab.turnBlocks.blocks
      .map((b) => b.tool?.input?.command)
      .filter(Boolean);
    expect(commands).toContain('git log --stat');
  });

  it('its terminal event settles it after the turn has finished', async () => {
    const p = mountPanel();
    const reqId = await backgroundTurn(p);
    pushEvent('subagent-event', {
      requestId: reqId,
      data: {
        task_id: 'task-1',
        agent_id: 'agent-1',
        status: 'completed',
        terminal: true,
        summary: 'listed 1 commit',
      },
    });
    await settle(p);
    const tab = subagentTab(p);
    expect(tab.subagent.settled).toBe(true);
    // Settled by its own outcome, so the LED reads completed rather than
    // amber — the whole point of not settling it early.
    expect(tab.subagent.unknown).toBe(false);
    expect(tab.subagent.status).toBe('completed');
  });

  it('a turn with nothing left running is torn down exactly as before', async () => {
    const p = mountPanel();
    const reqId = await startMainStream(p);
    pushEvent('subagent-event', {
      requestId: reqId,
      data: {
        task_id: 'task-1',
        agent_id: 'agent-1',
        description: 'quick look',
        task_type: 'local_agent',
        status: 'running',
      },
    });
    await settle(p);
    pushEvent('stream-complete', {
      requestId: reqId,
      result: {
        response: 'done',
        terminal_reason: 'completed',
        background_tasks: [],
      },
    });
    await settle(p);
    expect(p._tabs.get('main').currentRequestId).toBeNull();
    // No terminal event ever arrived for this one, and the turn really did
    // end it — so amber is the honest reading here.
    expect(subagentTab(p).subagent.unknown).toBe(true);
  });

  it('a completion with no background_tasks field tears the turn down', async () => {
    // Every synthetic footer the browser builds itself omits it, as does any
    // engine without the drain. An empty list has to be the safe reading.
    const p = mountPanel();
    const reqId = await startMainStream(p);
    pushEvent('stream-complete', {
      requestId: reqId,
      result: { response: 'done', terminal_reason: 'completed' },
    });
    await settle(p);
    expect(p._tabs.get('main').currentRequestId).toBeNull();
  });

  // -------------------------------------------------------------------
  // The turn's own follow-up answer
  // -------------------------------------------------------------------
  //
  // A task notification wakes main for a *further* turn on the same request,
  // and that turn ends in a result of its own flagged `continuation`. Before
  // the engine emitted those, main's closing answer was consumed, folded into
  // the turn's blocks, and rendered nowhere: the message had been frozen at
  // the first result and nothing re-froze it.

  /** Main answering the notification, then the drain-ending result. */
  async function continuation(panel, reqId, overrides = {}) {
    pushEvent('stream-chunk', {
      requestId: reqId,
      chunk: {
        block_id: 'blk_late',
        seq: 1,
        content: 'The background agent finished.',
        done: true,
      },
    });
    await settle(panel);
    pushEvent('stream-complete', {
      requestId: reqId,
      result: {
        response: 'dispatchedThe background agent finished.',
        terminal_reason: 'completed',
        continuation: true,
        background_tasks: [],
        ...overrides,
      },
    });
    await settle(panel);
  }

  it('main’s reply to a task notification reaches the transcript', async () => {
    const p = mountPanel();
    const reqId = await backgroundTurn(p);
    expect(p.messages.at(-1).content).toBe('dispatched');
    await continuation(p, reqId);
    expect(p.messages.at(-1).content).toContain('The background agent finished.');
  });

  it('a continuation revises the settled turn instead of appending one', async () => {
    const p = mountPanel();
    const reqId = await backgroundTurn(p);
    const before = p.messages.length;
    await continuation(p, reqId);
    // Every field on the payload is cumulative over the request, so a second
    // message would repeat the whole turn — the `Task` card and the subagent
    // row with it — under a second footer.
    expect(p.messages.length).toBe(before);
    expect(p.messages.filter((m) => m.role === 'assistant')).toHaveLength(1);
  });

  it('the revised turn keeps the wall-clock reading it was settled with', async () => {
    const p = mountPanel();
    const reqId = await backgroundTurn(p);
    const first = p.messages.at(-1).durationMs;
    expect(first).toEqual(expect.any(Number));
    await continuation(p, reqId);
    // The run timer stopped when the turn's presentation finished; there is no
    // second reading to take, and re-deriving one from a null start would drop
    // the ⏱ chip off a turn that had one.
    expect(p.messages.at(-1).durationMs).toBe(first);
  });

  it('the continuation carries the footer the turn ends up with', async () => {
    const p = mountPanel();
    const reqId = await backgroundTurn(p);
    await continuation(p, reqId, {
      turn_cost_usd: 0.24,
      turn_cost_basis: 'measured',
      tool_calls: 3,
    });
    // The engine prices every result of one turn against that turn's start
    // (`cost.py` § start_turn), so the last footer is the whole turn's cost —
    // most of which a background subagent spends after the first result.
    expect(p.messages.at(-1).turn.turn_cost_usd).toBe(0.24);
    expect(p.messages.at(-1).turn.tool_calls).toBe(3);
  });

  it('the continuation tears the turn down when nothing is left running', async () => {
    const p = mountPanel();
    const reqId = await backgroundTurn(p);
    expect(p._tabs.get('main').currentRequestId).toBe(reqId);
    await continuation(p, reqId);
    expect(p._tabs.get('main').currentRequestId).toBeNull();
    expect(p._tabs.get('main').turnBlocks.blocks).toHaveLength(0);
  });

  it('a continuation that leaves work running keeps the turn addressable', async () => {
    const p = mountPanel();
    const reqId = await backgroundTurn(p);
    // Main answered the first task's notification; a second task is still
    // going, so this is not the end of the run.
    await continuation(p, reqId, { background_tasks: ['task-2'] });
    expect(p.messages.at(-1).content).toContain('The background agent finished.');
    expect(p._tabs.get('main').currentRequestId).toBe(reqId);
  });

  it('a continuation for a turn with no settled message appends one', async () => {
    // The first result produced nothing to show, so there is no message to
    // revise. Appending is the only way its follow-up answer is seen at all.
    const p = mountPanel();
    const reqId = await startMainStream(p);
    pushEvent('stream-complete', {
      requestId: reqId,
      result: { response: '', terminal_reason: 'completed', background_tasks: ['task-1'] },
    });
    await settle(p);
    expect(p.messages.filter((m) => m.role === 'assistant')).toHaveLength(0);
    await continuation(p, reqId);
    expect(p.messages.at(-1).content).toContain('The background agent finished.');
  });
});

// The `onStreamRetry` suite stood here until conversion phase 3. It asserted
// the wording of a backoff toast fed by AIC⚡DC's own completion wrapper, which
// pushed `streamRetry` before each sleep. The CLI retries inside the
// subprocess without reporting it, so there is nothing left to assert; the
// `rateLimit` path is covered by the rate-limit notice tests.

// ---------------------------------------------------------------------------
// Last-completion outcome (LED row state)
// ---------------------------------------------------------------------------
//
// `computeLastEditOutcome(error, errorInfo, editResults)` read our own apply
// pipeline's `EditResult` list, classified anchor failures, and built a
// tooltip out of them. There is no such list: the agent owns its edits and
// reports them as tool results. `computeTurnOutcome(result, files)` replaces
// it and takes the engine's own verdict — `is_error`, `terminal_reason`,
// `cancelled` — with the turn's modified files standing in for the applied
// count, since "N files changed" is the question the tooltip was always
// answering.

describe('computeTurnOutcome — pure helper', () => {
  it('no result at all → clean / 0', () => {
    expect(computeTurnOutcome(null, null)).toEqual({
      status: 'clean',
      appliedCount: 0,
      failureReason: null,
    });
  });

  it('the modified-file count is the applied count', () => {
    const outcome = computeTurnOutcome({}, ['a.py', 'b.py', 'c.py']);
    expect(outcome.status).toBe('clean');
    expect(outcome.appliedCount).toBe(3);
    expect(outcome.failureReason).toBeNull();
  });

  it('a non-array file list counts as none', () => {
    expect(computeTurnOutcome({}, null).appliedCount).toBe(0);
    expect(computeTurnOutcome({}, 'oops').appliedCount).toBe(0);
    expect(computeTurnOutcome({}, undefined).appliedCount).toBe(0);
  });

  it('a cancelled turn is clean, and still counts its edits', () => {
    // The user stopped it. A red LED would read as a fault, and the files it
    // wrote before the stop are real.
    const outcome = computeTurnOutcome(
      { cancelled: true, terminal_reason: 'aborted_streaming' },
      ['a.py'],
    );
    expect(outcome.status).toBe('clean');
    expect(outcome.appliedCount).toBe(1);
    expect(outcome.failureReason).toBeNull();
  });

  it('is_error → error, reasoned from the engine errors list', () => {
    const outcome = computeTurnOutcome(
      { is_error: true, errors: ['rate limit exceeded'] },
      ['a.py'],
    );
    expect(outcome.status).toBe('error');
    // Partial credit — the tooltip still says what got written.
    expect(outcome.appliedCount).toBe(1);
    expect(outcome.failureReason).toBe('rate limit exceeded');
  });

  it('an API status is worth showing verbatim', () => {
    const outcome = computeTurnOutcome(
      { is_error: true, api_error_status: 529 },
      [],
    );
    expect(outcome.failureReason).toBe('API error 529');
  });

  it('falls back to the subtype, then to a generic phrase', () => {
    expect(
      computeTurnOutcome(
        { is_error: true, subtype: 'error_during_execution' },
        [],
      ).failureReason,
    ).toBe('error during execution');
    expect(
      computeTurnOutcome({ is_error: true }, []).failureReason,
    ).toBe('the turn failed');
  });

  it('a bad terminal reason is an error even without is_error', () => {
    // A turn that hit the turn limit did not do what was asked, and
    // `is_error` is false for it.
    const outcome = computeTurnOutcome(
      { terminal_reason: 'max_turns' },
      [],
    );
    expect(outcome.status).toBe('error');
    expect(outcome.failureReason).toBe('max turns');
  });

  it('terminal_reason "completed" is clean', () => {
    const outcome = computeTurnOutcome(
      { terminal_reason: 'completed' },
      ['a.py'],
    );
    expect(outcome.status).toBe('clean');
    expect(outcome.failureReason).toBeNull();
  });

  it('a permission denial is not a failure', () => {
    // The permission system working as designed. Red here would train users
    // to read the LED as noise.
    const outcome = computeTurnOutcome(
      {
        terminal_reason: 'completed',
        permission_denials: [{ tool_name: 'Bash' }],
      },
      [],
    );
    expect(outcome.status).toBe('clean');
  });

  it('is_error outranks a clean terminal reason', () => {
    const outcome = computeTurnOutcome(
      {
        is_error: true,
        terminal_reason: 'completed',
        errors: ['broke late'],
      },
      [],
    );
    expect(outcome.status).toBe('error');
    expect(outcome.failureReason).toBe('broke late');
  });
});

describe('ChatPanel onStreamComplete writes lastEditOutcome', () => {
  async function sendAndGetRequestId(panel, message = 'hi') {
    const started = vi
      .fn()
      .mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    await settle(panel);
    panel._input = message;
    await panel._send();
    return started.mock.calls[0][0];
  }

  it('clean completion → clean outcome on the active tab', async () => {
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('stream-complete', {
      requestId: reqId,
      result: {
        response: 'all done',
        terminal_reason: 'completed',
        files_modified: ['a.py', 'b.py'],
      },
    });
    await settle(p);
    const outcome = p._tabs.get('main').lastEditOutcome;
    expect(outcome).not.toBeNull();
    expect(outcome.status).toBe('clean');
    expect(outcome.appliedCount).toBe(2);
  });

  it('counts files the result omitted but a tool result reported', async () => {
    // The outcome is computed from the same union the footer renders, so a
    // turn that ended badly still says what it wrote.
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('tool-use', {
      requestId: reqId,
      data: {
        tool_use_id: 'toolu_1',
        name: 'Write',
        input: { file_path: 'x.py' },
      },
    });
    pushEvent('tool-result', {
      requestId: reqId,
      data: {
        tool_use_id: 'toolu_1',
        status: 'ok',
        files_modified: ['x.py'],
      },
    });
    pushEvent('stream-complete', {
      requestId: reqId,
      result: { response: 'wrote it', terminal_reason: 'completed' },
    });
    await settle(p);
    expect(
      p._tabs.get('main').lastEditOutcome.appliedCount,
    ).toBe(1);
  });

  it('an engine error → error outcome with the engine reason', async () => {
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('stream-complete', {
      requestId: reqId,
      result: {
        response: '',
        is_error: true,
        terminal_reason: 'engine_error',
        errors: ['rate limit exceeded'],
      },
    });
    await settle(p);
    const outcome = p._tabs.get('main').lastEditOutcome;
    expect(outcome.status).toBe('error');
    expect(outcome.failureReason).toBe('rate limit exceeded');
  });

  it('a stopped turn → clean outcome', async () => {
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('stream-complete', {
      requestId: reqId,
      result: {
        response: 'partial',
        cancelled: true,
        terminal_reason: 'aborted_streaming',
      },
    });
    await settle(p);
    expect(
      p._tabs.get('main').lastEditOutcome.status,
    ).toBe('clean');
  });

  it('an inactive tab gets its own outcome', async () => {
    const p = mountPanel();
    await settle(p);
    p._tabs.set('agent-0', p._makeTabState());
    p._tabLabels.set('agent-0', 'Agent 0');
    const agentTab = p._tabs.get('agent-0');
    agentTab.streaming = true;
    agentTab.currentRequestId = 'r-agent-1';
    p.requestUpdate();
    await settle(p);
    expect(p._activeTabId).toBe('main');
    pushEvent('stream-complete', {
      requestId: 'r-agent-1',
      result: {
        response: 'agent done',
        terminal_reason: 'completed',
        files_modified: ['x.py'],
      },
    });
    await settle(p);
    expect(agentTab.lastEditOutcome).not.toBeNull();
    expect(agentTab.lastEditOutcome.status).toBe('clean');
    expect(agentTab.lastEditOutcome.appliedCount).toBe(1);
    // The tab the user is looking at is untouched.
    expect(p._tabs.get('main').lastEditOutcome).toBeNull();
  });

  it('starts out null', async () => {
    const p = mountPanel();
    await settle(p);
    expect(p._tabs.get('main').lastEditOutcome).toBeNull();
  });

  it('the next completion overwrites it', async () => {
    const p = mountPanel();
    let reqId = await sendAndGetRequestId(p);
    pushEvent('stream-complete', {
      requestId: reqId,
      result: {
        response: 'first',
        is_error: true,
        errors: ['first failure'],
      },
    });
    await settle(p);
    expect(
      p._tabs.get('main').lastEditOutcome.status,
    ).toBe('error');
    reqId = await sendAndGetRequestId(p, 'second');
    pushEvent('stream-complete', {
      requestId: reqId,
      result: {
        response: 'second',
        terminal_reason: 'completed',
        files_modified: ['b.py'],
      },
    });
    await settle(p);
    const outcome = p._tabs.get('main').lastEditOutcome;
    expect(outcome.status).toBe('clean');
    expect(outcome.appliedCount).toBe(1);
  });
});

describe('ChatPanel stream-start error handling', () => {
  async function setupAgentTab(panel, tabId = 'frontend-trivial') {
    panel._tabs.set(tabId, panel._makeTabState());
    panel._tabLabels.set(tabId, tabId);
    panel._activeTabId = tabId;
    await settle(panel);
    return tabId;
  }

  // There is no `agent_tag`, so there is no stale one. The two tests that
  // used to live here drove `{error: "agent not found"}` and asserted the
  // panel closed the agent tab, switched to main, and called
  // `LLMService.close_agent_context`. `chat_streaming` cannot produce that
  // error any more — the service has no per-tab context to lose — and a
  // frontend that still closed tabs on an error string would delete a user's
  // conversation over the wording of an engine message. These pin the
  // inverse.

  it('an "agent not found" error leaves the tab alone', async () => {
    const chatStreaming = vi
      .fn()
      .mockResolvedValue({ error: 'agent not found' });
    const closeAgent = vi
      .fn()
      .mockResolvedValue({ status: 'ok', closed: false });
    publishFakeRpc({
      'ClaudeCodeService.chat_streaming': chatStreaming,
      'LLMService.close_agent_context': closeAgent,
    });
    const p = mountPanel();
    await settle(p);
    const tabId = await setupAgentTab(p);
    p._input = 'hello stale';
    await p._send();
    await settle(p);
    expect(p._tabs.has(tabId)).toBe(true);
    expect(p._activeTabId).toBe(tabId);
    expect(closeAgent).not.toHaveBeenCalled();
  });

  it('the optimistic user message stays where it was sent', async () => {
    // It used to be rolled back on the way to closing the tab. Now the turn
    // simply failed to start: the prompt the user wrote stays on the card
    // above the error, which is what makes the error legible.
    const chatStreaming = vi
      .fn()
      .mockResolvedValue({ error: 'agent not found' });
    publishFakeRpc({
      'ClaudeCodeService.chat_streaming': chatStreaming,
    });
    const p = mountPanel();
    await settle(p);
    const tabId = await setupAgentTab(p);
    p._input = 'stale message';
    await p._send();
    await settle(p);
    const tab = p._tabs.get(tabId);
    expect(tab.messages[0]).toMatchObject({
      role: 'user',
      content: 'stale message',
    });
    expect(tab.messages[1].content).toContain('agent not found');
    // And nothing leaked onto main.
    expect(p._tabs.get('main').messages).toEqual([]);
  });

  it('generic error appends assistant error message in current tab', async () => {
    const chatStreaming = vi.fn().mockResolvedValue({
      error: 'The engine is not connected',
    });
    publishFakeRpc({
      'ClaudeCodeService.chat_streaming': chatStreaming,
    });
    const p = mountPanel();
    await settle(p);
    p._input = 'hello';
    await p._send();
    await settle(p);
    expect(p.messages).toHaveLength(2);
    expect(p.messages[0].role).toBe('user');
    expect(p.messages[0].content).toBe('hello');
    expect(p.messages[1].role).toBe('assistant');
    expect(p.messages[1].content).toContain('not connected');
  });

  it('generic error clears streaming state', async () => {
    const chatStreaming = vi.fn().mockResolvedValue({
      error: 'The working directory is not a repository',
    });
    publishFakeRpc({
      'ClaudeCodeService.chat_streaming': chatStreaming,
    });
    const p = mountPanel();
    await settle(p);
    p._input = 'hi';
    await p._send();
    await settle(p);
    expect(p._streaming).toBe(false);
    expect(p._streamingContent).toBe('');
    expect(p._currentRequestId).toBeNull();
  });

  it('generic error on agent tab keeps the tab open', async () => {
    const chatStreaming = vi.fn().mockResolvedValue({
      error: 'The engine is not connected',
    });
    publishFakeRpc({
      'ClaudeCodeService.chat_streaming': chatStreaming,
    });
    const p = mountPanel();
    await settle(p);
    const tabId = await setupAgentTab(p);
    p._input = 'hi';
    await p._send();
    await settle(p);
    expect(p._tabs.has(tabId)).toBe(true);
    expect(p._activeTabId).toBe(tabId);
    const tab = p._tabs.get(tabId);
    expect(tab.messages).toHaveLength(2);
    expect(tab.messages[1].content).toContain('not connected');
  });

  it('"agent not found" on main tab does NOT close anything', async () => {
    const chatStreaming = vi
      .fn()
      .mockResolvedValue({ error: 'agent not found' });
    publishFakeRpc({
      'ClaudeCodeService.chat_streaming': chatStreaming,
    });
    const p = mountPanel();
    await settle(p);
    p._input = 'hi';
    await p._send();
    await settle(p);
    expect(p._tabs.has('main')).toBe(true);
    expect(p._activeTabId).toBe('main');
    expect(p.messages[1].content).toContain('agent not found');
  });

  it('RPC rejection still goes through the catch block', async () => {
    const chatStreaming = vi
      .fn()
      .mockRejectedValue(new Error('network died'));
    publishFakeRpc({
      'ClaudeCodeService.chat_streaming': chatStreaming,
    });
    const consoleSpy = vi
      .spyOn(console, 'error')
      .mockImplementation(() => {});
    try {
      const p = mountPanel();
      await settle(p);
      p._input = 'hi';
      await p._send();
      await settle(p);
      expect(p.messages[1].content).toContain('network died');
      expect(p._streaming).toBe(false);
    } finally {
      consoleSpy.mockRestore();
    }
  });

  it('a failed start marks the owning tab, not just the panel', async () => {
    // `send()` flips the panel-level flags, which are the *active* tab's
    // reactive surface. The per-tab outcome the LED row reads has to be
    // written too, or a turn that never started leaves the LED showing
    // whatever the previous turn did.
    const chatStreaming = vi
      .fn()
      .mockResolvedValue({ error: 'The engine is not connected' });
    publishFakeRpc({
      'ClaudeCodeService.chat_streaming': chatStreaming,
    });
    const p = mountPanel();
    await settle(p);
    const tabId = await setupAgentTab(p);
    p._input = 'hi';
    await p._send();
    await settle(p);
    const tab = p._tabs.get(tabId);
    expect(tab.streaming).toBe(false);
    expect(tab.currentRequestId).toBeNull();
    expect(tab.lastEditOutcome).toEqual({
      status: 'error',
      appliedCount: 0,
      failureReason: 'The engine is not connected',
    });
    // Blocks are reset — the turn produced none, and a stale card left over
    // from the previous turn would render under the error message.
    expect(tab.turnBlocks.blocks).toEqual([]);
  });

  it('a turn already in flight says what to do about it', async () => {
    // The one error message the handler rewrites. It fires when the user
    // sends before a post-reconnect resume has adopted the running turn, and
    // the useful half is the instruction, not the diagnosis.
    const chatStreaming = vi.fn().mockResolvedValue({
      error: 'A turn is already running (request req_1).',
      reason: 'turn_in_progress',
    });
    publishFakeRpc({
      'ClaudeCodeService.chat_streaming': chatStreaming,
    });
    const p = mountPanel();
    await settle(p);
    const toastListener = vi.fn();
    window.addEventListener('aic-toast', toastListener);
    try {
      p._input = 'hi';
      await p._send();
      await settle(p);
      expect(p.messages[1].content).toContain('already running');
      expect(p.messages[1].content).toContain('Stop');
      const warnings = toastListener.mock.calls
        .map((c) => c[0].detail)
        .filter((d) => d.type === 'warning');
      expect(warnings.length).toBeGreaterThan(0);
    } finally {
      window.removeEventListener('aic-toast', toastListener);
    }
  });
});
// ---------------------------------------------------------------------------
// The compaction pause, announced once
// ---------------------------------------------------------------------------

describe('ChatPanel compaction toast', () => {
  function toastsOf(panel) {
    const seen = [];
    panel._emitToast = (message, type) => seen.push([message, type]);
    return seen;
  }

  it('leaves the compaction pause to the progress overlay', async () => {
    // This used to toast. A toast lives 3 seconds and the compaction it
    // announces runs for tens, so the stall went unexplained for most of its
    // duration — the notice expired while the condition continued.
    // `aic-compaction-progress` listens to this same window event and holds an
    // indicator until `compact_boundary`; a toast on top of it would announce
    // one compaction twice with two different lifetimes.
    const p = mountPanel();
    await settle(p);
    const toasts = toastsOf(p);
    pushEvent('system-event', {
      requestId: null,
      data: { subtype: 'pre_compact', data: { trigger: 'auto' } },
    });
    await settle(p);
    expect(toasts).toEqual([]);
  });

  it('does not toast again when the hook reports itself', async () => {
    // `hookEvent` fires twice for one hook run — `hook_started` and then
    // `hook_response` — so a PreCompact branch there would double- or
    // triple-toast a single compaction now that the hook exists.
    const p = mountPanel();
    await settle(p);
    const toasts = toastsOf(p);
    for (const phase of ['hook_started', 'hook_response']) {
      pushEvent('hook-event', {
        requestId: null,
        data: { phase, hook_event_name: 'PreCompact' },
      });
    }
    await settle(p);
    expect(toasts).toEqual([]);
  });

  it('still toasts for a hook that blocked something', async () => {
    // The one hook-channel case that earns a word: it explains a tool call
    // the user is about to see fail.
    const p = mountPanel();
    await settle(p);
    const toasts = toastsOf(p);
    pushEvent('hook-event', {
      requestId: null,
      data: { hook_event_name: 'PreToolUse', outcome: 'block', tool_name: 'Bash' },
    });
    await settle(p);
    expect(toasts).toHaveLength(1);
    expect(toasts[0][0]).toContain('Bash');
    expect(toasts[0][1]).toBe('warning');
  });
});

// ---------------------------------------------------------------------------
// Live token counter
// ---------------------------------------------------------------------------

describe('ChatPanel live token counter', () => {
  async function sendAndGetRequestId(panel) {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    await settle(panel);
    panel._input = 'hi';
    await panel._send();
    return started.mock.calls[0][0];
  }

  function liveChips(panel) {
    const card = panel.shadowRoot.querySelector('.message-card.streaming');
    return [...(card?.querySelectorAll('.turn-live-usage .turn-usage') ?? [])]
      .map((s) => s.textContent.replace(/\s+/g, ' ').trim());
  }

  function usagePayload(reqId, models) {
    return { requestId: reqId, usage: { turn_model_usage: models } };
  }

  it('counts under the streaming card as the engine reports', async () => {
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('turn-usage', usagePayload(reqId, {
      'claude-opus-5': { input_tokens: 900, output_tokens: 100 },
    }));
    await settle(p);
    expect(liveChips(p)).toEqual(['claude-opus-5 1.0k tok · 900 in · 100 out']);
  });

  it('replaces the running figure rather than adding to it', async () => {
    // The engine accumulates server-side and pushes the whole turn's total
    // each time; summing here as well would double-count every step.
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('turn-usage', usagePayload(reqId, {
      'claude-opus-5': { input_tokens: 900, output_tokens: 100 },
    }));
    pushEvent('turn-usage', usagePayload(reqId, {
      'claude-opus-5': { input_tokens: 1800, output_tokens: 300 },
    }));
    await settle(p);
    expect(liveChips(p)).toEqual(['claude-opus-5 2.1k tok · 1.8k in · 300 out']);
  });

  it('ignores a counter for someone else’s turn', async () => {
    // A collaborator's stream carries its own request ID, and its tokens are
    // not this card's.
    const p = mountPanel();
    await sendAndGetRequestId(p);
    pushEvent('turn-usage', usagePayload('other-request-id', {
      'claude-opus-5': { input_tokens: 900 },
    }));
    await settle(p);
    expect(liveChips(p)).toEqual([]);
  });

  it('ignores a payload with no usable counters', async () => {
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    for (const usage of [null, {}, { turn_model_usage: 'claude' }]) {
      pushEvent('turn-usage', { requestId: reqId, usage });
    }
    pushEvent('turn-usage', { usage: { turn_model_usage: { m: { input_tokens: 5 } } } });
    await settle(p);
    expect(liveChips(p)).toEqual([]);
  });

  it('is gone once the settled footer carries the real figure', async () => {
    // Two counters for one turn, one of them stale, is worse than one.
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('turn-usage', usagePayload(reqId, {
      'claude-opus-5': { input_tokens: 900, output_tokens: 100 },
    }));
    await settle(p);
    expect(liveChips(p)).toHaveLength(1);
    pushEvent('stream-complete', {
      requestId: reqId,
      result: {
        response: 'final answer',
        terminal_reason: 'completed',
        turn_model_usage: { 'claude-opus-5': { inputTokens: 1000, outputTokens: 120 } },
      },
    });
    await settle(p);
    expect(p.shadowRoot.querySelector('.turn-live-usage')).toBeNull();
    // The footer's own chip is the engine's final word on the turn.
    expect(p.shadowRoot.querySelector('.turn-footer .turn-usage').textContent
      .replace(/\s+/g, ' ').trim())
      .toBe('claude-opus-5 1.1k tok · 1.0k in · 120 out');
  });

  it('starts the next turn from zero', async () => {
    // The counter is per-turn. Carrying it over would open a fresh turn
    // claiming tokens the previous one spent.
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('turn-usage', usagePayload(reqId, {
      'claude-opus-5': { input_tokens: 900, output_tokens: 100 },
    }));
    pushEvent('stream-complete', {
      requestId: reqId,
      result: { response: 'done', terminal_reason: 'completed' },
    });
    await settle(p);
    await sendAndGetRequestId(p);
    await settle(p);
    expect(liveChips(p)).toEqual([]);
  });

  it('survives a refresh mid-turn on the stream snapshot', async () => {
    // `active_streams[].usage` is why the engine accumulates server-side:
    // a browser that reloaded has no record of the messages already counted.
    const p = mountPanel();
    await settle(p);
    resumeActiveStreams(p, [{
      request_id: 'req-1',
      session_id: 'sess-1',
      started_at: 1,
      blocks: [{ block_id: 'req-1:b0', kind: 'text', seq: 0, content: 'Working.' }],
      subagents: [],
      usage: {
        turn_model_usage: { 'claude-opus-5': { input_tokens: 900, output_tokens: 100 } },
      },
    }]);
    await settle(p);
    expect(liveChips(p)).toEqual(['claude-opus-5 1.0k tok · 900 in · 100 out']);
  });

  it('stops counting once the panel is gone', async () => {
    // The handler is unbound with the rest of the channel; one left wired
    // holds the panel and its whole tab map alive after removal.
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    p.remove();
    pushEvent('turn-usage', usagePayload(reqId, {
      'claude-opus-5': { input_tokens: 900 },
    }));
    expect(p._tabs.get('main').turnBlocks.usage).toBeNull();
  });

  it('shows no counter for a snapshot from before the engine counted', async () => {
    const p = mountPanel();
    await settle(p);
    resumeActiveStreams(p, [{
      request_id: 'req-1',
      session_id: 'sess-1',
      started_at: 1,
      blocks: [{ block_id: 'req-1:b0', kind: 'text', seq: 0, content: 'Working.' }],
      subagents: [],
    }]);
    await settle(p);
    expect(liveChips(p)).toEqual([]);
  });
});
