// Tests for blocks.js — the block-keyed turn state behind a Claude Code turn.
//
// Plain functions over plain objects: no panel, no DOM, no RPC. That is the
// point of the module and it is why these tests read as a state machine
// exercise rather than a component one.
//
// What is worth pinning here is the set of properties the rest of the frontend
// leans on, each of which fails silently and confusingly if it regresses:
//
//   - Block identity, not array position, decides where a chunk lands.
//   - Content is cumulative *within* a block and never across a turn.
//   - A stale `seq` is discarded rather than applied.
//   - Freezing a turn copies deeply enough that a late event cannot rewrite
//     history the user has already read.
//   - A denial outranks an error, because a denied call also produces an
//     error-shaped result and "error" would hide who caused it.

import { describe, expect, it } from 'vitest';

import {
  applyPermissionOutcome,
  applyReplayBlocks,
  applySubagentEvent,
  applyToolResult,
  applyToolUse,
  collectFilesModified,
  collectToolPaths,
  drainChunks,
  freezeBlocks,
  isEmptyTurn,
  isTodoWrite,
  latestTodos,
  makeTurnBlocks,
  markAwaitingPermission,
  resetTurnBlocks,
  stageChunk,
  toolStatus,
} from './blocks.js';

/** Stage one text chunk and drain it, the way a frame callback would. */
function chunk(turn, blockId, content, seq = 0, extra = {}) {
  const staged = stageChunk(
    turn,
    { block_id: blockId, seq, content, done: false, ...extra },
    'text',
  );
  drainChunks(turn);
  return staged;
}

// ---------------------------------------------------------------------------
// Fresh state and reset
// ---------------------------------------------------------------------------

describe('makeTurnBlocks / resetTurnBlocks', () => {
  it('starts empty', () => {
    const turn = makeTurnBlocks();
    expect(turn.blocks).toEqual([]);
    expect(turn.index.size).toBe(0);
    expect(turn.pending.size).toBe(0);
    expect(turn.subagents.size).toBe(0);
    // Null, not an empty usage map: the engine has counted nothing yet, and
    // the live counter renders no chips at all rather than a row of zeroes.
    expect(turn.usage).toBeNull();
  });

  it('resets in place so every holder sees the same empty state', () => {
    // An in-flight rAF closure holds the same object the tab does. Replacing
    // it would leave the closure writing into an orphan.
    const turn = makeTurnBlocks();
    const blocks = turn.blocks;
    const index = turn.index;
    chunk(turn, 'r1:b0', 'hi');
    applySubagentEvent(turn, { task_id: 't1', description: 'go' });
    turn.usage = { turn_model_usage: { 'claude-opus-5': { input_tokens: 900 } } };
    resetTurnBlocks(turn);
    expect(turn.blocks).toBe(blocks);
    expect(turn.index).toBe(index);
    expect(turn.blocks).toHaveLength(0);
    expect(turn.index.size).toBe(0);
    expect(turn.subagents.size).toBe(0);
    // The token counter is per-turn, so it resets with everything else. A
    // carried-over figure would open the next turn already owing tokens.
    expect(turn.usage).toBeNull();
  });

  it('tolerates a missing turn', () => {
    expect(() => resetTurnBlocks(null)).not.toThrow();
    expect(() => resetTurnBlocks(undefined)).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// Text and thinking chunks
// ---------------------------------------------------------------------------

describe('stageChunk / drainChunks', () => {
  it('a first chunk creates its block', () => {
    const turn = makeTurnBlocks();
    expect(chunk(turn, 'r1:b0', 'Hello')).toBe(true);
    expect(turn.blocks).toHaveLength(1);
    expect(turn.blocks[0]).toMatchObject({
      block_id: 'r1:b0',
      kind: 'text',
      seq: 0,
      content: 'Hello',
      done: false,
    });
    expect(turn.index.get('r1:b0')).toBe(turn.blocks[0]);
  });

  it('seq 0 on a never-seen block is not stale', () => {
    // The distinction `highestSeq` returns null for. Treating "no record" as
    // -1 would work; treating it as 0 would drop every block's first chunk.
    const turn = makeTurnBlocks();
    expect(chunk(turn, 'r1:b0', 'first', 0)).toBe(true);
    expect(turn.blocks[0].content).toBe('first');
  });

  it('content replaces rather than concatenates within a block', () => {
    // The engine sends cumulative content per block. Appending would double
    // every token.
    const turn = makeTurnBlocks();
    chunk(turn, 'r1:b0', 'Hel');
    chunk(turn, 'r1:b0', 'Hello there', 1);
    expect(turn.blocks).toHaveLength(1);
    expect(turn.blocks[0].content).toBe('Hello there');
  });

  it('discards a chunk whose seq is not ahead of the applied one', () => {
    const turn = makeTurnBlocks();
    chunk(turn, 'r1:b0', 'newer', 5);
    expect(chunk(turn, 'r1:b0', 'older', 4)).toBe(false);
    expect(chunk(turn, 'r1:b0', 'same', 5)).toBe(false);
    expect(turn.blocks[0].content).toBe('newer');
  });

  it('discards a chunk behind one still staged', () => {
    // Two chunks in one frame: the high-water mark has to count the staged
    // entry, not just what has been drained.
    const turn = makeTurnBlocks();
    stageChunk(turn, { block_id: 'r1:b0', seq: 3, content: 'newer' }, 'text');
    expect(
      stageChunk(turn, { block_id: 'r1:b0', seq: 2, content: 'older' }, 'text'),
    ).toBe(false);
    drainChunks(turn);
    expect(turn.blocks[0].content).toBe('newer');
  });

  it('coalesces a burst into one applied chunk per block', () => {
    const turn = makeTurnBlocks();
    for (let seq = 0; seq < 20; seq += 1) {
      stageChunk(
        turn,
        { block_id: 'r1:b0', seq, content: `up to ${seq}` },
        'text',
      );
    }
    expect(turn.pending.size).toBe(1);
    expect(drainChunks(turn)).toBe(true);
    expect(turn.blocks).toHaveLength(1);
    expect(turn.blocks[0].content).toBe('up to 19');
    expect(turn.blocks[0].seq).toBe(19);
  });

  it('arrival order of first chunks is render order', () => {
    // Not sort order, and not block-id order — `b10` would sort before `b2`.
    const turn = makeTurnBlocks();
    chunk(turn, 'r1:b2', 'second');
    chunk(turn, 'r1:b10', 'third');
    chunk(turn, 'r1:b0', 'first');
    expect(turn.blocks.map((b) => b.block_id)).toEqual([
      'r1:b2',
      'r1:b10',
      'r1:b0',
    ]);
  });

  it('records the thinking kind, and treats anything else as text', () => {
    const turn = makeTurnBlocks();
    stageChunk(turn, { block_id: 'r1:b0', seq: 0, content: 'hm' }, 'thinking');
    stageChunk(turn, { block_id: 'r1:b1', seq: 0, content: 'hi' }, 'wat');
    drainChunks(turn);
    expect(turn.blocks[0].kind).toBe('thinking');
    expect(turn.blocks[1].kind).toBe('text');
  });

  it('carries `done` through the drain', () => {
    const turn = makeTurnBlocks();
    stageChunk(
      turn,
      { block_id: 'r1:b0', seq: 0, content: 'all of it', done: true },
      'text',
    );
    drainChunks(turn);
    expect(turn.blocks[0].done).toBe(true);
  });

  it('normalises a missing or empty agent id to null', () => {
    const turn = makeTurnBlocks();
    chunk(turn, 'r1:b0', 'main');
    chunk(turn, 'r1:b1', 'blank', 0, { agent_id: '' });
    chunk(turn, 'r1:b2', 'sub', 0, { agent_id: 'toolu_task' });
    expect(turn.blocks.map((b) => b.agent_id)).toEqual([
      null,
      null,
      'toolu_task',
    ]);
  });

  it('refuses an unroutable chunk instead of inventing a block', () => {
    // No `block_id` is a protocol break. Making one up would put text in the
    // wrong place and hide the break.
    const turn = makeTurnBlocks();
    for (const payload of [
      null,
      undefined,
      'text',
      {},
      { seq: 0, content: 'orphan' },
      { block_id: '', content: 'orphan' },
      { block_id: 7, content: 'orphan' },
    ]) {
      expect(stageChunk(turn, payload, 'text')).toBe(false);
    }
    expect(stageChunk(null, { block_id: 'r1:b0' }, 'text')).toBe(false);
    expect(turn.blocks).toHaveLength(0);
  });

  it('non-numeric seq is read as 0', () => {
    const turn = makeTurnBlocks();
    stageChunk(turn, { block_id: 'r1:b0', content: 'no seq' }, 'text');
    drainChunks(turn);
    expect(turn.blocks[0].seq).toBe(0);
  });

  it('an empty frame reports no change', () => {
    const turn = makeTurnBlocks();
    expect(drainChunks(turn)).toBe(false);
    expect(drainChunks(null)).toBe(false);
  });

  it('a chunk superseded between staging and draining is skipped', () => {
    // `stageChunk` guards this, so the only way in is a direct write to
    // `pending` — which is what a caller that stages from two paths could do.
    // The drain re-checks rather than trusting the stage.
    const turn = makeTurnBlocks();
    chunk(turn, 'r1:b0', 'applied', 9);
    turn.pending.set('r1:b0', {
      kind: 'text',
      seq: 4,
      content: 'stale',
      done: false,
      agentId: null,
    });
    expect(drainChunks(turn)).toBe(false);
    expect(turn.blocks[0].content).toBe('applied');
    expect(turn.pending.size).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// Freezing
// ---------------------------------------------------------------------------

describe('freezeBlocks', () => {
  it('copies each block so the live turn cannot rewrite it', () => {
    const turn = makeTurnBlocks();
    chunk(turn, 'r1:b0', 'the answer');
    const frozen = freezeBlocks(turn);
    turn.blocks[0].content = 'something else';
    expect(frozen[0].content).toBe('the answer');
    expect(frozen[0]).not.toBe(turn.blocks[0]);
  });

  it('copies tool and result one level deeper', () => {
    // The shallow copy alone would share the `tool` object, and a late result
    // arriving after the freeze would mutate the settled card.
    const turn = makeTurnBlocks();
    applyToolUse(turn, { tool_use_id: 'toolu_1', name: 'Read' });
    applyToolResult(turn, { tool_use_id: 'toolu_1', status: 'ok' });
    const frozen = freezeBlocks(turn);
    turn.blocks[0].tool.name = 'Write';
    turn.blocks[0].result.status = 'error';
    expect(frozen[0].tool.name).toBe('Read');
    expect(frozen[0].result.status).toBe('ok');
    expect(frozen[0].tool).not.toBe(turn.blocks[0].tool);
    expect(frozen[0].result).not.toBe(turn.blocks[0].result);
  });

  it('leaves a null result null rather than copying it into an object', () => {
    const turn = makeTurnBlocks();
    applyToolUse(turn, { tool_use_id: 'toolu_1', name: 'Read' });
    expect(freezeBlocks(turn)[0].result).toBeNull();
  });

  it('freezes nothing from no turn', () => {
    expect(freezeBlocks(null)).toEqual([]);
    expect(freezeBlocks(makeTurnBlocks())).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Tool cards
// ---------------------------------------------------------------------------

describe('applyToolUse', () => {
  it('appends a card that starts out pending', () => {
    const turn = makeTurnBlocks();
    expect(
      applyToolUse(turn, {
        tool_use_id: 'toolu_1',
        name: 'Bash',
        input: { command: 'ls' },
      }),
    ).toBe(true);
    expect(turn.blocks).toHaveLength(1);
    expect(turn.blocks[0]).toMatchObject({
      block_id: 'toolu_1',
      kind: 'tool',
      result: null,
      gated: false,
      denial: null,
      superseded: false,
    });
    expect(toolStatus(turn.blocks[0])).toBe('pending');
  });

  it('is idempotent by tool_use_id', () => {
    // A reconnect replay followed by the live event is a legitimate second
    // delivery. Appending a twin would show the call twice.
    const turn = makeTurnBlocks();
    applyToolUse(turn, { tool_use_id: 'toolu_1', name: 'Bash' });
    applyToolUse(turn, {
      tool_use_id: 'toolu_1',
      name: 'Bash',
      input: { command: 'ls' },
    });
    expect(turn.blocks).toHaveLength(1);
    expect(turn.blocks[0].tool.input).toEqual({ command: 'ls' });
  });

  it('merges rather than replaces on redelivery', () => {
    const turn = makeTurnBlocks();
    applyToolUse(turn, {
      tool_use_id: 'toolu_1',
      name: 'Bash',
      input: { command: 'ls' },
    });
    applyToolUse(turn, { tool_use_id: 'toolu_1', status: 'ok' });
    expect(turn.blocks[0].tool).toMatchObject({
      name: 'Bash',
      input: { command: 'ls' },
      status: 'ok',
    });
  });

  it('a redelivery without an agent id does not blank the one we had', () => {
    const turn = makeTurnBlocks();
    applyToolUse(turn, {
      tool_use_id: 'toolu_1',
      name: 'Read',
      agent_id: 'toolu_task',
    });
    applyToolUse(turn, { tool_use_id: 'toolu_1', status: 'ok' });
    expect(turn.blocks[0].agent_id).toBe('toolu_task');
  });

  it('carries the engine’s gated flag', () => {
    // The control request can beat the assistant message, in which case the
    // card arrives already marked.
    const turn = makeTurnBlocks();
    applyToolUse(turn, { tool_use_id: 'toolu_1', name: 'Write', gated: true });
    expect(turn.blocks[0].gated).toBe(true);
  });

  it('refuses a card with no tool_use_id', () => {
    const turn = makeTurnBlocks();
    for (const card of [null, undefined, 'Bash', {}, { tool_use_id: '' }]) {
      expect(applyToolUse(turn, card)).toBe(false);
    }
    expect(applyToolUse(null, { tool_use_id: 'toolu_1' })).toBe(false);
    expect(turn.blocks).toHaveLength(0);
  });

  it('a new TodoWrite supersedes earlier ones without removing them', () => {
    // Removing would renumber block order. The renderer skips superseded
    // cards instead.
    const turn = makeTurnBlocks();
    applyToolUse(turn, { tool_use_id: 't1', name: 'TodoWrite' });
    applyToolUse(turn, { tool_use_id: 'r1', name: 'Read' });
    applyToolUse(turn, { tool_use_id: 't2', name: 'TodoWrite' });
    expect(turn.blocks.map((b) => b.superseded)).toEqual([
      true,
      false,
      false,
    ]);
    expect(turn.blocks).toHaveLength(3);
  });

  it('an MCP-qualified TodoWrite supersedes the built-in one', () => {
    const turn = makeTurnBlocks();
    applyToolUse(turn, { tool_use_id: 't1', name: 'TodoWrite' });
    applyToolUse(turn, {
      tool_use_id: 't2',
      name: 'mcp__planner__TodoWrite',
    });
    expect(turn.blocks[0].superseded).toBe(true);
    expect(turn.blocks[1].superseded).toBe(false);
  });

  it('a redelivered TodoWrite does not supersede itself', () => {
    const turn = makeTurnBlocks();
    applyToolUse(turn, { tool_use_id: 't1', name: 'TodoWrite' });
    applyToolUse(turn, { tool_use_id: 't1', name: 'TodoWrite' });
    expect(turn.blocks).toHaveLength(1);
    expect(turn.blocks[0].superseded).toBe(false);
  });
});

describe('applyToolResult', () => {
  it('attaches to its card and finishes it', () => {
    const turn = makeTurnBlocks();
    applyToolUse(turn, { tool_use_id: 'toolu_1', name: 'Read' });
    expect(
      applyToolResult(turn, {
        tool_use_id: 'toolu_1',
        status: 'ok',
        preview: 'file contents',
      }),
    ).toBe(true);
    expect(turn.blocks[0].done).toBe(true);
    expect(turn.blocks[0].result.preview).toBe('file contents');
    expect(toolStatus(turn.blocks[0])).toBe('ok');
  });

  it('records an error status on the card as well as the result', () => {
    const turn = makeTurnBlocks();
    applyToolUse(turn, { tool_use_id: 'toolu_1', name: 'Bash' });
    applyToolResult(turn, { tool_use_id: 'toolu_1', status: 'error' });
    expect(turn.blocks[0].tool.status).toBe('error');
    expect(toolStatus(turn.blocks[0])).toBe('error');
  });

  it('any status that is not "error" settles as ok', () => {
    const turn = makeTurnBlocks();
    applyToolUse(turn, { tool_use_id: 'toolu_1', name: 'Bash' });
    applyToolResult(turn, { tool_use_id: 'toolu_1' });
    expect(turn.blocks[0].tool.status).toBe('ok');
  });

  it('drops a result with no card to attach it to', () => {
    // A floating result would render as a card with no header, which reads as
    // a rendering bug rather than a missed message.
    const turn = makeTurnBlocks();
    expect(
      applyToolResult(turn, { tool_use_id: 'toolu_missing', status: 'ok' }),
    ).toBe(false);
    expect(turn.blocks).toHaveLength(0);
  });

  it('will not attach a result to a text block', () => {
    const turn = makeTurnBlocks();
    chunk(turn, 'r1:b0', 'prose');
    expect(
      applyToolResult(turn, { tool_use_id: 'r1:b0', status: 'ok' }),
    ).toBe(false);
    expect(turn.blocks[0].result).toBeUndefined();
  });

  it('refuses a malformed payload', () => {
    const turn = makeTurnBlocks();
    for (const payload of [null, undefined, 'ok', {}, { tool_use_id: '' }]) {
      expect(applyToolResult(turn, payload)).toBe(false);
    }
    expect(applyToolResult(null, { tool_use_id: 'x' })).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Permission state on a card
// ---------------------------------------------------------------------------

describe('markAwaitingPermission', () => {
  it('locks the card while the engine waits on a human', () => {
    const turn = makeTurnBlocks();
    applyToolUse(turn, { tool_use_id: 'toolu_1', name: 'Write' });
    expect(markAwaitingPermission(turn, 'toolu_1')).toBe(true);
    expect(turn.blocks[0]).toMatchObject({ gated: true, awaiting: true });
    expect(toolStatus(turn.blocks[0])).toBe('awaiting');
  });

  it('does nothing when the card has not arrived yet', () => {
    // The control request can beat the assistant message describing it.
    const turn = makeTurnBlocks();
    expect(markAwaitingPermission(turn, 'toolu_1')).toBe(false);
  });

  it('refuses a malformed id or a non-tool block', () => {
    const turn = makeTurnBlocks();
    chunk(turn, 'r1:b0', 'prose');
    expect(markAwaitingPermission(turn, 'r1:b0')).toBe(false);
    expect(markAwaitingPermission(turn, '')).toBe(false);
    expect(markAwaitingPermission(turn, null)).toBe(false);
    expect(markAwaitingPermission(null, 'toolu_1')).toBe(false);
  });
});

describe('applyPermissionOutcome', () => {
  function gatedTurn() {
    const turn = makeTurnBlocks();
    applyToolUse(turn, { tool_use_id: 'toolu_1', name: 'Write' });
    markAwaitingPermission(turn, 'toolu_1');
    return turn;
  }

  it('allow clears the lock but keeps the gated marker', () => {
    // The transcript records that the user authorised the call — that is what
    // the marker is for, so it outlives the dialog.
    const turn = gatedTurn();
    expect(
      applyPermissionOutcome(turn, {
        tool_use_id: 'toolu_1',
        action: 'allow',
      }),
    ).toBe(true);
    expect(turn.blocks[0]).toMatchObject({
      gated: true,
      awaiting: false,
      denial: null,
    });
    expect(toolStatus(turn.blocks[0])).toBe('pending');
  });

  it('deny records the reason the agent also saw', () => {
    const turn = gatedTurn();
    applyPermissionOutcome(turn, {
      tool_use_id: 'toolu_1',
      action: 'deny',
      reason: 'not that file',
      resolved_by: 'matt',
    });
    expect(turn.blocks[0].denial).toEqual({
      action: 'deny',
      reason: 'not that file',
      resolvedBy: 'matt',
    });
    expect(toolStatus(turn.blocks[0])).toBe('denied');
  });

  it('every allow action clears the lock, not just the plain one', () => {
    // The bug this pins: the test used to be `action === 'allow'`, so a call
    // the user approved with "always allow" — or with the session mode
    // switch — rendered as *denied*, amber lock and denial body, on a call
    // that ran. Adding an allow action to the engine without adding it to
    // ALLOW_ACTIONS reintroduces exactly that.
    for (const action of ['allow', 'allow_always', 'allow_mode']) {
      const turn = gatedTurn();
      applyPermissionOutcome(turn, { tool_use_id: 'toolu_1', action });
      expect(turn.blocks[0].denial).toBeNull();
      expect(toolStatus(turn.blocks[0])).toBe('pending');
    }
  });

  it('anything that is not an allow is a denial, verbatim', () => {
    // Timeout and shutdown are denials with their own names, and the name is
    // the difference between "you said no" and "nobody answered".
    for (const action of ['timeout', 'shutdown', 'cancelled']) {
      const turn = gatedTurn();
      applyPermissionOutcome(turn, {
        tool_use_id: 'toolu_1',
        action,
      });
      expect(turn.blocks[0].denial.action).toBe(action);
      expect(toolStatus(turn.blocks[0])).toBe('denied');
    }
  });

  it('a missing action defaults to deny and missing strings to empty', () => {
    const turn = gatedTurn();
    applyPermissionOutcome(turn, { tool_use_id: 'toolu_1' });
    expect(turn.blocks[0].denial).toEqual({
      action: 'deny',
      reason: '',
      resolvedBy: '',
    });
  });

  it('marks a card gated even if no dialog was seen locally', () => {
    // A collaborator's client may have opened the dialog. The outcome
    // broadcast still has to leave this card looking gated.
    const turn = makeTurnBlocks();
    applyToolUse(turn, { tool_use_id: 'toolu_1', name: 'Write' });
    applyPermissionOutcome(turn, {
      tool_use_id: 'toolu_1',
      action: 'allow',
    });
    expect(turn.blocks[0].gated).toBe(true);
  });

  it('refuses a malformed payload or an unknown card', () => {
    const turn = gatedTurn();
    for (const payload of [null, undefined, 'allow', {}, { tool_use_id: '' }]) {
      expect(applyPermissionOutcome(turn, payload)).toBe(false);
    }
    expect(
      applyPermissionOutcome(turn, { tool_use_id: 'nope', action: 'deny' }),
    ).toBe(false);
    expect(
      applyPermissionOutcome(null, { tool_use_id: 'toolu_1' }),
    ).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Status precedence
// ---------------------------------------------------------------------------

describe('toolStatus', () => {
  it('a denial outranks the error-shaped result it produced', () => {
    // The engine reports a denied call as an error result. Rendering "error"
    // would hide that the user caused it.
    const turn = makeTurnBlocks();
    applyToolUse(turn, { tool_use_id: 'toolu_1', name: 'Write' });
    applyPermissionOutcome(turn, {
      tool_use_id: 'toolu_1',
      action: 'deny',
      reason: 'no',
    });
    applyToolResult(turn, { tool_use_id: 'toolu_1', status: 'error' });
    expect(toolStatus(turn.blocks[0])).toBe('denied');
  });

  it('awaiting outranks the card’s own status', () => {
    const turn = makeTurnBlocks();
    applyToolUse(turn, {
      tool_use_id: 'toolu_1',
      name: 'Write',
      status: 'ok',
    });
    markAwaitingPermission(turn, 'toolu_1');
    expect(toolStatus(turn.blocks[0])).toBe('awaiting');
  });

  it('reads the result status ahead of the card status', () => {
    const turn = makeTurnBlocks();
    applyToolUse(turn, {
      tool_use_id: 'toolu_1',
      name: 'Bash',
      status: 'ok',
    });
    turn.blocks[0].result = { status: 'error' };
    expect(toolStatus(turn.blocks[0])).toBe('error');
  });

  it('an unrecognised status is pending, not an error', () => {
    const turn = makeTurnBlocks();
    applyToolUse(turn, {
      tool_use_id: 'toolu_1',
      name: 'Bash',
      status: 'running',
    });
    expect(toolStatus(turn.blocks[0])).toBe('pending');
  });

  it('is pending for anything that is not a tool block', () => {
    expect(toolStatus(null)).toBe('pending');
    expect(toolStatus({ kind: 'text' })).toBe('pending');
  });
});

// ---------------------------------------------------------------------------
// Subagent rows
// ---------------------------------------------------------------------------

describe('applySubagentEvent', () => {
  it('keys a row by task_id', () => {
    const turn = makeTurnBlocks();
    expect(
      applySubagentEvent(turn, {
        task_id: 'task_1',
        description: 'refactor auth',
        task_type: 'general-purpose',
        status: 'running',
      }),
    ).toBe(true);
    expect(turn.subagents.size).toBe(1);
    expect(turn.subagents.get('task_1')).toMatchObject({
      key: 'task_1',
      description: 'refactor auth',
      task_type: 'general-purpose',
      status: 'running',
      terminal: false,
    });
  });

  it('patches rather than replaces as fields arrive piecemeal', () => {
    // A progress event with no description must not blank the description the
    // started event supplied.
    const turn = makeTurnBlocks();
    applySubagentEvent(turn, {
      task_id: 'task_1',
      description: 'refactor auth',
      task_type: 'general-purpose',
    });
    applySubagentEvent(turn, {
      task_id: 'task_1',
      status: 'running',
      last_tool_name: 'Grep',
    });
    expect(turn.subagents.get('task_1')).toMatchObject({
      description: 'refactor auth',
      task_type: 'general-purpose',
      status: 'running',
      last_tool_name: 'Grep',
    });
  });

  it('an agent id arriving later fills the row instead of opening a second', () => {
    // `agent_id` is the transcript key but the CLI reports it in the payload
    // rather than the dataclass, so it can be absent on the first event.
    const turn = makeTurnBlocks();
    applySubagentEvent(turn, { task_id: 'task_1', description: 'go' });
    applySubagentEvent(turn, { task_id: 'task_1', agent_id: 'agent_7' });
    expect(turn.subagents.size).toBe(1);
    expect(turn.subagents.get('task_1').agent_id).toBe('agent_7');
  });

  it('latches terminal from an update with no notification', () => {
    // `stop_task` reports `killed` this way. Waiting for a notification would
    // leave the row spinning forever.
    const turn = makeTurnBlocks();
    applySubagentEvent(turn, { task_id: 'task_1', status: 'running' });
    applySubagentEvent(turn, {
      task_id: 'task_1',
      status: 'killed',
      terminal: true,
    });
    applySubagentEvent(turn, { task_id: 'task_1', last_tool_name: 'Read' });
    expect(turn.subagents.get('task_1').terminal).toBe(true);
  });

  it('falls back to agent_id then tool_use_id for the key', () => {
    const turn = makeTurnBlocks();
    applySubagentEvent(turn, { agent_id: 'agent_7', description: 'a' });
    applySubagentEvent(turn, { tool_use_id: 'toolu_task', description: 'b' });
    expect([...turn.subagents.keys()]).toEqual(['agent_7', 'toolu_task']);
  });

  it('records the spawning Task call so the row can nest under it', () => {
    const turn = makeTurnBlocks();
    applySubagentEvent(turn, {
      task_id: 'task_1',
      tool_use_id: 'toolu_task',
    });
    expect(turn.subagents.get('task_1').tool_use_id).toBe('toolu_task');
  });

  it('refuses an event with nothing to key on', () => {
    const turn = makeTurnBlocks();
    for (const payload of [
      null,
      undefined,
      'started',
      {},
      { status: 'running' },
      { task_id: '', agent_id: '', tool_use_id: '' },
    ]) {
      expect(applySubagentEvent(turn, payload)).toBe(false);
    }
    expect(applySubagentEvent(null, { task_id: 't' })).toBe(false);
    expect(turn.subagents.size).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// Reconnect replay
// ---------------------------------------------------------------------------

describe('applyReplayBlocks', () => {
  it('rebuilds text, thinking and tool blocks in order', () => {
    const turn = makeTurnBlocks();
    expect(
      applyReplayBlocks(turn, [
        { block_id: 'r1:b0', kind: 'thinking', seq: 2, content: 'weighing' },
        { block_id: 'r1:b1', kind: 'text', seq: 5, content: 'here goes' },
        {
          block_id: 'toolu_1',
          kind: 'tool',
          seq: 0,
          tool: { tool_use_id: 'toolu_1', name: 'Read' },
        },
      ]),
    ).toBe(true);
    expect(turn.blocks.map((b) => [b.kind, b.seq])).toEqual([
      ['thinking', 2],
      ['text', 5],
      ['tool', 0],
    ]);
    expect(turn.index.get('toolu_1').tool.name).toBe('Read');
  });

  it('adopts the snapshot seq as the high-water mark', () => {
    // So the next live chunk is compared against something real rather than
    // overwriting the replayed content with an older frame.
    const turn = makeTurnBlocks();
    applyReplayBlocks(turn, [
      { block_id: 'r1:b0', kind: 'text', seq: 12, content: 'replayed' },
    ]);
    expect(chunk(turn, 'r1:b0', 'stale', 11)).toBe(false);
    expect(turn.blocks[0].content).toBe('replayed');
    expect(chunk(turn, 'r1:b0', 'live', 13)).toBe(true);
    expect(turn.blocks[0].content).toBe('live');
  });

  it('replaces whatever the turn held', () => {
    const turn = makeTurnBlocks();
    chunk(turn, 'r1:bX', 'from before the refresh');
    applyReplayBlocks(turn, [
      { block_id: 'r1:b0', kind: 'text', seq: 0, content: 'snapshot' },
    ]);
    expect(turn.blocks.map((b) => b.block_id)).toEqual(['r1:b0']);
    expect(turn.index.has('r1:bX')).toBe(false);
  });

  it('marks a block done only where the payload proves it', () => {
    // A mid-turn snapshot is unfinished by definition and carries no `done`
    // flag; a tool card with a result is the one thing that does prove it.
    const turn = makeTurnBlocks();
    applyReplayBlocks(turn, [
      { block_id: 'r1:b0', kind: 'text', seq: 0, content: 'partial' },
      {
        block_id: 'toolu_1',
        kind: 'tool',
        tool: { tool_use_id: 'toolu_1', name: 'Read', result: { status: 'ok' } },
      },
      {
        block_id: 'toolu_2',
        kind: 'tool',
        tool: { tool_use_id: 'toolu_2', name: 'Bash' },
      },
    ]);
    expect(turn.blocks.map((b) => b.done)).toEqual([false, true, false]);
    expect(turn.blocks[1].result).toEqual({ status: 'ok' });
    expect(turn.blocks[2].result).toBeNull();
  });

  it('keeps the scope that produced each block', () => {
    // A subagent narrates in text as well as calling tools, and after a
    // refresh that text still has to render under its row — and in its own
    // tab — rather than as something Main said.
    const turn = makeTurnBlocks();
    applyReplayBlocks(turn, [
      { block_id: 'r1:b0', kind: 'text', content: 'Delegating.' },
      {
        block_id: 'r1:b1',
        kind: 'text',
        content: 'reading the parser',
        agent_id: 'toolu_task',
      },
      {
        block_id: 'toolu_9',
        kind: 'tool',
        tool: { tool_use_id: 'toolu_9', name: 'Grep', agent_id: 'toolu_task' },
      },
    ]);
    expect(turn.blocks.map((b) => b.agent_id)).toEqual([
      null,
      'toolu_task',
      'toolu_task',
    ]);
  });

  it('resolves the latest TodoWrite in one pass', () => {
    const turn = makeTurnBlocks();
    applyReplayBlocks(turn, [
      { block_id: 't1', kind: 'tool', tool: { name: 'TodoWrite' } },
      { block_id: 't2', kind: 'tool', tool: { name: 'TodoWrite' } },
      { block_id: 't3', kind: 'tool', tool: { name: 'TodoWrite' } },
    ]);
    expect(turn.blocks.map((b) => b.superseded)).toEqual([
      true,
      true,
      false,
    ]);
  });

  it('falls back to the block id when a card carries no tool_use_id', () => {
    const turn = makeTurnBlocks();
    applyReplayBlocks(turn, [
      { block_id: 'toolu_1', kind: 'tool', tool: { name: 'Read' } },
    ]);
    expect(turn.blocks[0].tool.tool_use_id).toBe('toolu_1');
  });

  it('reads an unknown kind as text', () => {
    const turn = makeTurnBlocks();
    applyReplayBlocks(turn, [
      { block_id: 'r1:b0', kind: 'diagram', content: 'who knows' },
    ]);
    expect(turn.blocks[0].kind).toBe('text');
  });

  it('skips unusable records and duplicate ids', () => {
    const turn = makeTurnBlocks();
    applyReplayBlocks(turn, [
      null,
      'text',
      { kind: 'text', content: 'no id' },
      { block_id: '', kind: 'text' },
      { block_id: 'r1:b0', kind: 'text', content: 'first' },
      { block_id: 'r1:b0', kind: 'text', content: 'second' },
    ]);
    expect(turn.blocks).toHaveLength(1);
    expect(turn.blocks[0].content).toBe('first');
  });

  it('an empty or absent snapshot leaves the turn empty', () => {
    const turn = makeTurnBlocks();
    chunk(turn, 'r1:b0', 'before');
    expect(applyReplayBlocks(turn, [])).toBe(false);
    expect(turn.blocks).toHaveLength(0);
    expect(applyReplayBlocks(turn, null)).toBe(false);
    expect(applyReplayBlocks(null, [])).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Derived views
// ---------------------------------------------------------------------------

describe('collectFilesModified', () => {
  it('lists each file once, in first-seen order', () => {
    const blocks = [
      { kind: 'tool', result: { files_modified: ['b.py', 'a.py'] } },
      { kind: 'tool', result: { files_modified: ['a.py', 'c.py'] } },
    ];
    expect(collectFilesModified(blocks)).toEqual(['b.py', 'a.py', 'c.py']);
  });

  it('ignores blocks with nothing to report', () => {
    expect(
      collectFilesModified([
        { kind: 'text', content: 'src/foo.py' },
        { kind: 'tool', result: null },
        { kind: 'tool', result: { files_modified: 'a.py' } },
        { kind: 'tool', result: { files_modified: ['', null, 7, 'ok.py'] } },
        null,
      ]),
    ).toEqual(['ok.py']);
  });

  it('reads nothing from a non-array', () => {
    expect(collectFilesModified(null)).toEqual([]);
    expect(collectFilesModified('a.py')).toEqual([]);
  });
});

describe('collectToolPaths', () => {
  it('unions the files written with the paths the calls named', () => {
    const blocks = [
      {
        kind: 'tool',
        tool: { name: 'Read', input: { file_path: 'src/read.py' } },
        result: {},
      },
      {
        kind: 'tool',
        tool: { name: 'Edit', input: { file_path: 'src/edit.py' } },
        result: { files_modified: ['src/edit.py'] },
      },
      {
        kind: 'tool',
        tool: { name: 'Glob', input: { path: 'src/' } },
      },
      {
        kind: 'tool',
        tool: {
          name: 'NotebookEdit',
          input: { notebook_path: 'nb.ipynb' },
        },
      },
    ];
    expect(collectToolPaths(blocks).sort()).toEqual([
      'nb.ipynb',
      'src/',
      'src/edit.py',
      'src/read.py',
    ]);
  });

  it('ignores non-tool blocks and unusable inputs', () => {
    expect(
      collectToolPaths([
        { kind: 'text', content: 'src/mentioned.py' },
        { kind: 'tool', tool: { name: 'Bash', input: null } },
        { kind: 'tool', tool: { name: 'Bash', input: 'ls' } },
        { kind: 'tool', tool: { name: 'Read', input: { file_path: '' } } },
      ]),
    ).toEqual([]);
    expect(collectToolPaths(null)).toEqual([]);
  });
});

describe('latestTodos', () => {
  it('returns the newest call’s items', () => {
    const blocks = [
      {
        kind: 'tool',
        tool: { name: 'TodoWrite', input: { todos: [{ content: 'old' }] } },
      },
      { kind: 'tool', tool: { name: 'Read', input: {} } },
      {
        kind: 'tool',
        tool: {
          name: 'TodoWrite',
          input: { todos: [{ content: 'new' }, { content: 'newer' }] },
        },
      },
    ];
    expect(latestTodos(blocks)).toEqual([
      { content: 'new' },
      { content: 'newer' },
    ]);
  });

  it('filters items that are not objects', () => {
    expect(
      latestTodos([
        {
          kind: 'tool',
          tool: {
            name: 'TodoWrite',
            input: { todos: [{ content: 'real' }, null, 'string'] },
          },
        },
      ]),
    ).toEqual([{ content: 'real' }]);
  });

  it('stops at the newest call even when its todos are unusable', () => {
    // Falling through to an older call would show a stale plan as the live
    // one, which is worse than showing none.
    expect(
      latestTodos([
        {
          kind: 'tool',
          tool: { name: 'TodoWrite', input: { todos: [{ content: 'old' }] } },
        },
        { kind: 'tool', tool: { name: 'TodoWrite', input: {} } },
      ]),
    ).toBeNull();
  });

  it('is null for a turn with no plan', () => {
    // A turn short enough not to need a plan should not grow an empty
    // checklist.
    expect(latestTodos([{ kind: 'text', content: 'done' }])).toBeNull();
    expect(latestTodos([])).toBeNull();
    expect(latestTodos(null)).toBeNull();
  });
});

describe('isEmptyTurn', () => {
  it('is true between request accepted and the first block', () => {
    expect(isEmptyTurn(null)).toBe(true);
    expect(isEmptyTurn(makeTurnBlocks())).toBe(true);
  });

  it('a staged chunk already counts as something to show', () => {
    // The waiting indicator has to give way on the first chunk, not on the
    // first drained frame.
    const turn = makeTurnBlocks();
    stageChunk(turn, { block_id: 'r1:b0', seq: 0, content: 'H' }, 'text');
    expect(isEmptyTurn(turn)).toBe(false);
  });

  it('is false once a block exists', () => {
    const turn = makeTurnBlocks();
    chunk(turn, 'r1:b0', 'Hi');
    expect(isEmptyTurn(turn)).toBe(false);
  });

  it('a subagent row alone does not count', () => {
    // Rows render nested under the `Task` card that spawned them, so a row
    // with no blocks has nowhere to appear.
    const turn = makeTurnBlocks();
    applySubagentEvent(turn, { task_id: 'task_1', description: 'go' });
    expect(isEmptyTurn(turn)).toBe(true);
  });
});

describe('isTodoWrite', () => {
  it('matches the built-in and MCP-qualified names', () => {
    expect(isTodoWrite('TodoWrite')).toBe(true);
    expect(isTodoWrite('mcp__planner__TodoWrite')).toBe(true);
  });

  it('does not match a lookalike', () => {
    for (const name of [
      'TodoRead',
      'TodoWriteAll',
      'todowrite',
      '',
      null,
      undefined,
      7,
    ]) {
      expect(isTodoWrite(name)).toBe(false);
    }
  });
});
