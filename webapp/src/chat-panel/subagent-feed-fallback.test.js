// Tests for the transcript fallback on a subagent tab that mirrored nothing.
//
// A live subagent tab's feed is normally the parent turn's blocks filtered to
// that subagent, mirrored by reference — no read, per
// specs5/5-webapp/subagent-browser.md § Refresh and Reconnect. That holds only
// while the blocks arrive on the turn the tab belongs to. A *background*
// subagent outlives its turn, so its later output is translated against
// whichever turn is current (§ Tab Lifetime, Known gap) and the tab it left
// behind has nothing to mirror: a label over an empty feed, which reads as
// "this subagent did nothing".
//
// So a settled subagent tab with an empty feed reads its own transcript when
// the user opens it. The conditions are the interesting part and most of what
// is asserted here — settled, empty, once, and only with an `agent_id`.

import { describe, expect, it, vi } from 'vitest';

import { loadSubagentFeedIfEmpty } from './tabs.js';
import { makeTabState } from './state.js';
import { mountPanel, publishFakeRpc, settle } from './test-helpers.js';

const TRANSCRIPT = [
  { role: 'user', content: 'run the probe' },
  {
    role: 'assistant',
    content: 'Probe finished clean.',
    blocks: [
      {
        block_id: 'b0',
        kind: 'text',
        seq: 0,
        content: 'Probe finished clean.',
        done: true,
        agent_id: null,
      },
    ],
    subagents: [],
    turn: { tool_calls: 2, num_turns: 1, files_modified: [] },
  },
];

/**
 * A settled subagent tab with an empty feed — the shape a background subagent
 * leaves behind once its turn has moved on.
 */
function seedSubagentTab(panel, over = {}) {
  const tab = makeTabState();
  tab.readOnly = true;
  tab.messages = [
    {
      role: 'user',
      content: '🤖 Explore — run the probe',
      system_event: true,
      subagent_seed: true,
    },
  ];
  tab.subagent = {
    key: 'task-1',
    task_id: 'task-1',
    agent_id: 'agent_abc',
    tool_use_id: 'toolu_task',
    description: 'run the probe',
    settled: true,
    terminal: true,
    status: 'completed',
    ...over,
  };
  panel._tabs.set('agent_abc', tab);
  panel._tabLabels.set('agent_abc', '1 probe');
  return tab;
}

describe('an empty subagent tab fills from its transcript', () => {
  it('reads by agent id and appends after the seed line', async () => {
    const read = vi.fn().mockResolvedValue(TRANSCRIPT);
    publishFakeRpc({ 'ClaudeCodeService.get_subagent_transcript': read });
    const p = mountPanel();
    await settle(p);
    const tab = seedSubagentTab(p);

    expect(await loadSubagentFeedIfEmpty(p, 'agent_abc')).toBe(true);
    expect(read.mock.calls[0][0]).toBe('agent_abc');
    // The seed survives: it says what the subagent was asked to do, which the
    // transcript's own first message does not necessarily repeat.
    expect(tab.messages[0].subagent_seed).toBe(true);
    expect(tab.messages.map((m) => m.role)).toEqual([
      'user',
      'user',
      'assistant',
    ]);
    expect(tab.messages[2].blocks[0].content).toBe('Probe finished clean.');
  });

  it('fires when the user activates the tab, by any route in', async () => {
    const read = vi.fn().mockResolvedValue(TRANSCRIPT);
    publishFakeRpc({ 'ClaudeCodeService.get_subagent_transcript': read });
    const p = mountPanel();
    await settle(p);
    seedSubagentTab(p);

    // The setter, not `onTabClick` — so the LED row, the overflow menu and the
    // Alt+` shortcut all reach it too.
    p._activeTabId = 'agent_abc';
    await settle(p);
    expect(read).toHaveBeenCalledTimes(1);
  });

  it('says why in place of the messages when the read fails', async () => {
    const read = vi
      .fn()
      .mockResolvedValue({ error: 'Subagent agent_abc has no readable transcript' });
    publishFakeRpc({ 'ClaudeCodeService.get_subagent_transcript': read });
    const p = mountPanel();
    await settle(p);
    const tab = seedSubagentTab(p);

    await loadSubagentFeedIfEmpty(p, 'agent_abc');
    // § Empty States: the reason renders as a system note, and the tab stays.
    expect(tab.messages[1].content).toContain('no readable transcript');
    expect(tab.messages[1].system_event).toBe(true);
    expect(p._tabs.has('agent_abc')).toBe(true);
  });
});

describe('when the fallback stays out of the way', () => {
  it('leaves a live subagent alone — its blocks may still be coming', async () => {
    const read = vi.fn().mockResolvedValue(TRANSCRIPT);
    publishFakeRpc({ 'ClaudeCodeService.get_subagent_transcript': read });
    const p = mountPanel();
    await settle(p);
    seedSubagentTab(p, { settled: false, terminal: false, status: null });

    expect(await loadSubagentFeedIfEmpty(p, 'agent_abc')).toBe(false);
    expect(read).not.toHaveBeenCalled();
  });

  it('leaves a tab that mirrored blocks alone — those are the better view', async () => {
    const read = vi.fn().mockResolvedValue(TRANSCRIPT);
    publishFakeRpc({ 'ClaudeCodeService.get_subagent_transcript': read });
    const p = mountPanel();
    await settle(p);
    const tab = seedSubagentTab(p);
    const block = { block_id: 'b1', kind: 'text', content: 'live', agent_id: 'toolu_task' };
    tab.turnBlocks.blocks.push(block);
    tab.turnBlocks.index.set('b1', block);

    expect(await loadSubagentFeedIfEmpty(p, 'agent_abc')).toBe(false);
    expect(read).not.toHaveBeenCalled();
  });

  it('reads once however often the tab is opened', async () => {
    const read = vi.fn().mockResolvedValue(TRANSCRIPT);
    publishFakeRpc({ 'ClaudeCodeService.get_subagent_transcript': read });
    const p = mountPanel();
    await settle(p);
    seedSubagentTab(p);

    p._activeTabId = 'agent_abc';
    p._activeTabId = 'main';
    p._activeTabId = 'agent_abc';
    await settle(p);
    expect(read).toHaveBeenCalledTimes(1);
  });

  it('cannot read a row that only ever reported a task id', async () => {
    const read = vi.fn().mockResolvedValue(TRANSCRIPT);
    publishFakeRpc({ 'ClaudeCodeService.get_subagent_transcript': read });
    const p = mountPanel();
    await settle(p);
    // Observed live: `agent_id` is null on every event of some runs, and the
    // tab is keyed on the task id instead. `get_subagent_transcript` reads by
    // agent id, and the panel does not guess one from the session listing —
    // "the panel never invents a tab" covers its contents too.
    seedSubagentTab(p, { agent_id: null });

    expect(await loadSubagentFeedIfEmpty(p, 'agent_abc')).toBe(false);
    expect(read).not.toHaveBeenCalled();
  });

  it('does not touch Main, or a transcript tab read off disk', async () => {
    const read = vi.fn().mockResolvedValue(TRANSCRIPT);
    publishFakeRpc({ 'ClaudeCodeService.get_subagent_transcript': read });
    const p = mountPanel();
    await settle(p);
    const historical = makeTabState();
    historical.readOnly = true;
    p._tabs.set('historical:agent_abc', historical);

    expect(await loadSubagentFeedIfEmpty(p, 'main')).toBe(false);
    expect(await loadSubagentFeedIfEmpty(p, 'historical:agent_abc')).toBe(false);
    expect(read).not.toHaveBeenCalled();
  });

  it('drops a read whose tab left the strip while it was out', async () => {
    let resolve;
    const read = vi.fn().mockReturnValue(new Promise((r) => { resolve = r; }));
    publishFakeRpc({ 'ClaudeCodeService.get_subagent_transcript': read });
    const p = mountPanel();
    await settle(p);
    const tab = seedSubagentTab(p);

    const pending = loadSubagentFeedIfEmpty(p, 'agent_abc');
    // A new turn clears the subagent tabs — § Tab Lifetime.
    p._tabs.delete('agent_abc');
    resolve(TRANSCRIPT);
    expect(await pending).toBe(false);
    expect(tab.messages).toHaveLength(1);
  });
});
