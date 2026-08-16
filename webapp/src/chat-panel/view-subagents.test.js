// Tests for the two ways into a subagent's transcript from the chat: the
// "View subagents (N)" affordance beneath a settled turn, and the subagent
// row's own description.
//
// Both dispatch `view-subagents-requested` carrying `{agents: [{agent_id,
// label}]}`, which is the whole contract with `tabs.js` — the handler's own
// tests live in view-subagents-load.test.js.
//
// The affordance is gated on the turn having rows that name an agent and on
// at least one of them no longer being live in the strip. A turn read back
// off disk carries no rows at all, which is a deliberate absence rather than
// a bug: the transcript does not record which turn spawned which subagent.

import { describe, expect, it, vi } from 'vitest';

import {
  mountPanel,
  publishFakeRpc,
  pushEvent,
  seedTab,
  settle,
} from './test-helpers.js';

/** A settled Claude Code turn with `rows` as its subagents. */
function turn(rows) {
  return {
    role: 'assistant',
    content: 'delegated the search',
    blocks: [
      {
        block_id: 'b0',
        kind: 'text',
        seq: 0,
        content: 'delegated the search',
        done: true,
        agent_id: null,
      },
    ],
    subagents: rows,
    turn: { tool_calls: 1, num_turns: 1, files_modified: [] },
  };
}

function row(overrides = {}) {
  return {
    key: 'task-1',
    agent_id: 'agent_abc',
    description: 'find auth call sites',
    task_type: 'explore',
    status: 'completed',
    terminal: true,
    ...overrides,
  };
}

describe('View-subagents affordance — visibility', () => {
  it('renders beneath a turn whose subagents have left the strip', async () => {
    const p = mountPanel({ messages: [turn([row()])] });
    await settle(p);
    const button = p.shadowRoot.querySelector('.view-subagents-button');
    expect(button).not.toBeNull();
    expect(button.textContent).toContain('View subagent (1)');
  });

  it('counts the subagents in the label', async () => {
    const p = mountPanel({
      messages: [
        turn([
          row(),
          row({ key: 'task-2', agent_id: 'agent_def' }),
          row({ key: 'task-3', agent_id: 'agent_ghi' }),
        ]),
      ],
    });
    await settle(p);
    expect(
      p.shadowRoot.querySelector('.view-subagents-button').textContent,
    ).toContain('View subagents (3)');
  });

  it('does not render on a turn that spawned nothing', async () => {
    const p = mountPanel({ messages: [turn([])] });
    await settle(p);
    expect(
      p.shadowRoot.querySelector('.view-subagents-affordance'),
    ).toBeNull();
  });

  it('does not render on a turn read back off disk', async () => {
    // `history_load` freezes `subagents: []` — the transcript does not
    // record which turn spawned which subagent — so a resumed session
    // offers nothing here, by omission rather than by oversight. The
    // history browser is the way into an older session's subagents.
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', { messages: [turn([])] });
    await settle(p);
    expect(p.shadowRoot.querySelector('.turn-footer')).toBeTruthy();
    expect(
      p.shadowRoot.querySelector('.view-subagents-affordance'),
    ).toBeNull();
  });

  it('ignores a row that names no agent', async () => {
    // A row keyed only by task id has no transcript to read.
    const p = mountPanel({
      messages: [turn([row({ agent_id: undefined })])],
    });
    await settle(p);
    expect(
      p.shadowRoot.querySelector('.view-subagents-affordance'),
    ).toBeNull();
  });

  it('does not render when every subagent is still live in the strip', async () => {
    const p = mountPanel({ messages: [turn([row()])] });
    await settle(p);
    seedTab(p, 'agent_abc');
    p.requestUpdate();
    await settle(p);
    expect(
      p.shadowRoot.querySelector('.view-subagents-affordance'),
    ).toBeNull();
  });

  it('renders when only some subagents are still live', async () => {
    const p = mountPanel({
      messages: [turn([row(), row({ key: 'task-2', agent_id: 'agent_def' })])],
    });
    await settle(p);
    seedTab(p, 'agent_abc');
    p.requestUpdate();
    await settle(p);
    // Both are offered — the handler is what skips the live one, so the
    // count matches the turn rather than the strip.
    expect(
      p.shadowRoot.querySelector('.view-subagents-button').textContent,
    ).toContain('View subagents (2)');
  });

  it('does not render on a plain assistant message', async () => {
    // No `blocks`, so not a Claude Code turn at all.
    const p = mountPanel({
      messages: [{ role: 'assistant', content: 'no tools here' }],
    });
    await settle(p);
    expect(
      p.shadowRoot.querySelector('.view-subagents-affordance'),
    ).toBeNull();
  });
});

describe('View-subagents affordance — dispatch', () => {
  it('carries every agent id and its label', async () => {
    const p = mountPanel({
      messages: [
        turn([
          row(),
          row({
            key: 'task-2',
            agent_id: 'agent_def',
            description: 'write the tests',
            task_type: 'general',
          }),
        ]),
      ],
    });
    await settle(p);
    const seen = vi.fn();
    p.addEventListener('view-subagents-requested', seen);
    p.shadowRoot.querySelector('.view-subagents-button').click();
    expect(seen).toHaveBeenCalledOnce();
    expect(seen.mock.calls[0][0].detail).toEqual({
      agents: [
        { agent_id: 'agent_abc', label: 'explore: find auth call sites' },
        { agent_id: 'agent_def', label: 'general: write the tests' },
      ],
    });
  });

  it('bubbles and composes out of the shadow root', async () => {
    const p = mountPanel({ messages: [turn([row()])] });
    await settle(p);
    const seen = vi.fn();
    document.body.addEventListener('view-subagents-requested', seen);
    try {
      p.shadowRoot.querySelector('.view-subagents-button').click();
      expect(seen).toHaveBeenCalledOnce();
    } finally {
      document.body.removeEventListener('view-subagents-requested', seen);
    }
  });
});

describe('Subagent row — opening one transcript', () => {
  it('dispatches for that row alone', async () => {
    const p = mountPanel({
      messages: [
        turn([row(), row({ key: 'task-2', agent_id: 'agent_def' })]),
      ],
    });
    await settle(p);
    const seen = vi.fn();
    p.addEventListener('view-subagents-requested', seen);
    const rows = p.shadowRoot.querySelectorAll('.subagent-desc-button');
    expect(rows).toHaveLength(2);
    rows[1].click();
    expect(seen.mock.calls[0][0].detail).toEqual({
      agents: [
        { agent_id: 'agent_def', label: 'explore: find auth call sites' },
      ],
    });
  });

  it('leaves a row that names no agent as plain text', async () => {
    const p = mountPanel({
      messages: [turn([row({ agent_id: undefined })])],
    });
    await settle(p);
    expect(
      p.shadowRoot.querySelector('.subagent-desc-button'),
    ).toBeNull();
    expect(
      p.shadowRoot.querySelector('.subagent-desc').textContent.trim(),
    ).toBe('find auth call sites');
  });
});
