// Tests for live subagent tabs: one strip button per running `Task`, the feed
// mirrored into it, what settles it, and what clears it.
//
// The behaviour under test is the one the spec asked for and the panel did not
// do: a subagent used to appear only as an indented row inside Main's turn, so
// two feeds shared one message list. The row is still there — these tests pin
// that too, because the tab is an addition rather than a replacement.
//
// Governing spec: specs5/5-webapp/subagent-browser.md.

import { describe, expect, it, vi } from 'vitest';

import { resumeActiveStreams } from './events.js';
import { getLedState } from './led-row.js';
import {
  subagentKeyword,
  subagentLedState,
  subagentLedTooltip,
} from './subagent-tabs.js';
import {
  mountPanel,
  publishFakeRpc,
  pushEvent,
  settle,
} from './test-helpers.js';

// The `Task` call that spawned the subagent. Blocks produced inside it carry
// this as their `agent_id`, which is the only thing joining feed to tab.
const PARENT = 'toolu_task';

async function sendAndGetRequestId(panel, message = 'hi') {
  const started = vi.fn().mockResolvedValue({ status: 'started' });
  publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
  await settle(panel);
  panel._input = message;
  await panel._send();
  return started.mock.calls[0][0];
}

function subagentEvent(reqId, over = {}) {
  return {
    requestId: reqId,
    data: {
      type: 'started',
      task_id: 'task-1',
      agent_id: 'agent-1',
      tool_use_id: PARENT,
      description: 'audit the parser',
      subagent_type: 'Explore',
      terminal: false,
      ...over,
    },
  };
}

function feedChunk(reqId, content, seq = 0, agentId = PARENT) {
  return {
    requestId: reqId,
    chunk: {
      block_id: `${reqId}:b9`,
      seq,
      content,
      done: false,
      agent_id: agentId,
    },
  };
}

/** Start a turn, spawn one subagent, and hand back its tab. */
async function withSubagent(panel, over = {}) {
  const reqId = await sendAndGetRequestId(panel);
  pushEvent('subagent-event', subagentEvent(reqId, over));
  await settle(panel);
  return { reqId, tab: panel._tabs.get('agent-1') };
}

// ---------------------------------------------------------------------------
// The tab
// ---------------------------------------------------------------------------

describe('a live subagent gets a tab', () => {
  it('opens one, keyed on its agent id', async () => {
    const p = mountPanel();
    const { tab } = await withSubagent(p);
    expect(p._tabs.size).toBe(2);
    expect(tab).toBeTruthy();
    expect(tab.streaming).toBe(true);
    // No channel to a subagent, live or finished — hence no input surface.
    expect(tab.readOnly).toBe(true);
    // Not the parent's request id: `findTabForRequest` scans that field and a
    // second claimant would steal Main's chunks.
    expect(tab.currentRequestId).toBeFalsy();
    expect(p._activeTabId).toBe('main');
  });

  it('labels the strip button with an ordinal and a keyword', async () => {
    // A 40-character label costs the *other* tabs their visibility, so the
    // strip carries `1 parser` and the sentence moves to the tooltip.
    const p = mountPanel();
    await withSubagent(p);
    expect(p._tabLabels.get('agent-1')).toBe('1 parser');
    const btn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="agent-1"]',
    );
    expect(btn).toBeTruthy();
    expect(btn.textContent).toContain('1 parser');
    expect(btn.title).toBe('1 · Explore — audit the parser — subagent feed (read-only)');
  });

  it('opens with the description, not an empty message list', async () => {
    // The feed gets the whole sentence: it is the one place with room for it.
    const p = mountPanel();
    const { tab } = await withSubagent(p);
    expect(tab.messages[0].content).toBe('🤖 Explore — audit the parser');
    expect(tab.messages[0].system_event).toBe(true);
  });

  it('leaves the row in Main alone — the tab is an addition', async () => {
    const p = mountPanel();
    await withSubagent(p);
    // Main is still the active tab, so these accessors read its turn.
    expect(p._turnBlocks.subagents.size).toBe(1);
  });

  it('a second event for the same task updates rather than duplicates',
    async () => {
      const p = mountPanel();
      const reqId = await sendAndGetRequestId(p);
      // A `started` that named no description — the tab has only an id to
      // label itself with.
      pushEvent('subagent-event', subagentEvent(reqId, {
        description: '',
        subagent_type: '',
      }));
      await settle(p);
      expect(p._tabs.size).toBe(2);
      expect(p._tabLabels.get('agent-1')).toBe('1 agent-1');
      // ...and the description arrives on the next message.
      pushEvent('subagent-event', subagentEvent(reqId, {
        type: 'progress',
        last_tool_name: 'Grep',
      }));
      await settle(p);
      expect(p._tabs.size).toBe(2);
      expect(p._tabLabels.get('agent-1')).toBe('1 parser');
      // The opening line is rewritten in place rather than appended twice.
      const tab = p._tabs.get('agent-1');
      expect(tab.messages.filter((m) => m.subagent_seed)).toHaveLength(1);
      expect(tab.messages[0].content).toBe('🤖 Explore — audit the parser');
    });

  it('numbers the tabs in creation order, and the number sticks', async () => {
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('subagent-event', subagentEvent(reqId));
    pushEvent('subagent-event', subagentEvent(reqId, {
      task_id: 'task-2',
      agent_id: 'agent-2',
      tool_use_id: 'toolu_task2',
      description: 'check the tests',
    }));
    await settle(p);
    expect(p._tabLabels.get('agent-1')).toBe('1 parser');
    expect(p._tabLabels.get('agent-2')).toBe('2 check-tests');
    // The first one finishes. The second must not be renumbered under the
    // user's cursor, and the first keeps the number it was given.
    pushEvent('subagent-event', subagentEvent(reqId, {
      type: 'notification',
      status: 'completed',
      terminal: true,
    }));
    await settle(p);
    expect(p._tabLabels.get('agent-1')).toBe('1 parser');
    expect(p._tabLabels.get('agent-2')).toBe('2 check-tests');
  });

  it('two subagents get two tabs', async () => {
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('subagent-event', subagentEvent(reqId));
    pushEvent('subagent-event', subagentEvent(reqId, {
      task_id: 'task-2',
      agent_id: 'agent-2',
      tool_use_id: 'toolu_task2',
      description: 'check the tests',
    }));
    await settle(p);
    expect(Array.from(p._tabs.keys())).toEqual(['main', 'agent-1', 'agent-2']);
  });
});

// ---------------------------------------------------------------------------
// The feed
// ---------------------------------------------------------------------------

describe('the subagent feed', () => {
  it('holds the very same block records Main does', async () => {
    // One card object in two placements: `blocks.js` mutates records in place,
    // so a result landing on a card has to appear in both without a second
    // write.
    const p = mountPanel();
    const { reqId } = await withSubagent(p);
    pushEvent('stream-chunk', feedChunk(reqId, 'looking'));
    await settle(p);
    const mine = p._tabs.get('agent-1').turnBlocks.blocks;
    const main = p._tabs.get('main').turnBlocks.blocks;
    expect(mine).toHaveLength(1);
    expect(mine[0]).toBe(main.find((b) => b.agent_id === PARENT));
    expect(mine[0].content).toBe('looking');
  });

  it('a later chunk for a mirrored block is not mirrored twice', async () => {
    const p = mountPanel();
    const { reqId } = await withSubagent(p);
    pushEvent('stream-chunk', feedChunk(reqId, 'look', 0));
    await settle(p);
    pushEvent('stream-chunk', feedChunk(reqId, 'looking', 1));
    await settle(p);
    const mine = p._tabs.get('agent-1').turnBlocks.blocks;
    expect(mine).toHaveLength(1);
    expect(mine[0].content).toBe('looking');
  });

  it('a tool result mutating a card shows up in both places', async () => {
    const p = mountPanel();
    const { reqId } = await withSubagent(p);
    pushEvent('tool-use', {
      requestId: reqId,
      data: {
        tool_use_id: 'toolu_read',
        name: 'Read',
        input: { file_path: 'a.py' },
        agent_id: PARENT,
      },
    });
    await settle(p);
    pushEvent('tool-result', {
      requestId: reqId,
      data: { tool_use_id: 'toolu_read', status: 'ok' },
    });
    await settle(p);
    const mine = p._tabs.get('agent-1').turnBlocks.blocks;
    expect(mine).toHaveLength(1);
    expect(mine[0].result.status).toBe('ok');
  });

  it("does not claim Main's own blocks", async () => {
    const p = mountPanel();
    const { reqId } = await withSubagent(p);
    pushEvent('stream-chunk', {
      requestId: reqId,
      chunk: { block_id: `${reqId}:b0`, seq: 0, content: 'thinking out loud' },
    });
    await settle(p);
    expect(p._tabs.get('agent-1').turnBlocks.blocks).toHaveLength(0);
  });

  it('renders flat, with no row header and no composer', async () => {
    const p = mountPanel();
    const { reqId } = await withSubagent(p);
    pushEvent('stream-chunk', feedChunk(reqId, 'looking'));
    await settle(p);
    p.shadowRoot
      .querySelector('.tab-strip-tab[data-tab-id="agent-1"]')
      .click();
    await settle(p);
    const messages = p.shadowRoot.querySelector('.messages');
    expect(messages.textContent).toContain('looking');
    // A row header inside a tab that is entirely about that one subagent
    // would wrap the feed in a description it already carries.
    expect(p.shadowRoot.querySelector('.subagent-row')).toBeNull();
    expect(p.shadowRoot.querySelector('.read-only-note')).toBeTruthy();
    expect(p.shadowRoot.querySelector('textarea')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Settling
// ---------------------------------------------------------------------------

describe('a subagent that finishes', () => {
  it('keeps its tab, stops its feed', async () => {
    const p = mountPanel();
    const { reqId } = await withSubagent(p);
    pushEvent('stream-chunk', feedChunk(reqId, 'done looking'));
    await settle(p);
    pushEvent('subagent-event', subagentEvent(reqId, {
      type: 'notification',
      status: 'completed',
      summary: 'three call sites',
      usage: { total_tokens: 1200, tool_uses: 4 },
      terminal: true,
    }));
    await settle(p);
    const tab = p._tabs.get('agent-1');
    expect(tab).toBeTruthy();
    expect(tab.streaming).toBe(false);
    expect(tab.streamStartedAt).toBeNull();
    expect(getLedState(tab)).toBe('green');
    // The feed is on a message now that no streaming card draws it, and it
    // still shares the live array so a trailing tool result lands in it.
    const last = tab.messages[tab.messages.length - 1];
    expect(last.blocks).toBe(tab.turnBlocks.blocks);
    expect(last.blocks[0].content).toBe('done looking');
  });

  it('is created and settled at once when we only see the terminal event',
    async () => {
      const p = mountPanel();
      const { tab } = await withSubagent(p, {
        type: 'notification',
        status: 'completed',
        terminal: true,
      });
      expect(tab).toBeTruthy();
      expect(tab.streaming).toBe(false);
      expect(getLedState(tab)).toBe('green');
    });

  it('a block arriving after it settled still reaches the feed', async () => {
    // The subagent's last tool call racing its own terminal status.
    const p = mountPanel();
    const { reqId } = await withSubagent(p, {
      type: 'notification',
      status: 'completed',
      terminal: true,
    });
    pushEvent('stream-chunk', feedChunk(reqId, 'one last thing'));
    await settle(p);
    const tab = p._tabs.get('agent-1');
    const last = tab.messages[tab.messages.length - 1];
    expect(last.blocks).toBe(tab.turnBlocks.blocks);
    expect(last.blocks[0].content).toBe('one last thing');
  });
});

describe('a subagent still live when the turn ends', () => {
  it('reports an unknown outcome rather than a completed one', async () => {
    const p = mountPanel();
    const { reqId } = await withSubagent(p);
    pushEvent('stream-complete', {
      requestId: reqId,
      result: { response: 'done', terminal_reason: 'completed' },
    });
    await settle(p);
    const tab = p._tabs.get('agent-1');
    expect(tab).toBeTruthy();
    expect(tab.subagent.settled).toBe(true);
    expect(tab.subagent.unknown).toBe(true);
    expect(tab.streaming).toBe(false);
    expect(getLedState(tab)).toBe('amber');
    expect(subagentLedTooltip(tab.subagent, 'amber')).toContain(
      'status unknown at turn end',
    );
  });

  it('turns red when the turn itself failed', async () => {
    const p = mountPanel();
    const { reqId } = await withSubagent(p);
    pushEvent('stream-complete', {
      requestId: reqId,
      result: { response: '', is_error: true, errors: ['engine died'] },
    });
    await settle(p);
    expect(getLedState(p._tabs.get('agent-1'))).toBe('red');
  });

  it('stays green when it had already reported completing', async () => {
    const p = mountPanel();
    const { reqId } = await withSubagent(p, {
      type: 'notification',
      status: 'completed',
      terminal: true,
    });
    pushEvent('stream-complete', {
      requestId: reqId,
      result: { response: '', is_error: true, errors: ['engine died'] },
    });
    await settle(p);
    const tab = p._tabs.get('agent-1');
    expect(tab.subagent.unknown).toBe(false);
    expect(getLedState(tab)).toBe('green');
  });
});

// ---------------------------------------------------------------------------
// Stop
// ---------------------------------------------------------------------------

describe('the ⏹ Stop affordance', () => {
  it('replaces the 📊 Context icon while the subagent runs', async () => {
    const p = mountPanel();
    await withSubagent(p);
    const btn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="agent-1"]',
    );
    expect(btn.querySelector('.tab-stop')).toBeTruthy();
    // A subagent has no context window of its own; its tokens are the
    // parent turn's.
    expect(btn.querySelector('.tab-context')).toBeNull();
  });

  it('confirms before stopping, and sends the task id', async () => {
    const p = mountPanel();
    const { tab } = await withSubagent(p);
    p._stopSubagent = vi.fn();
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    p.shadowRoot
      .querySelector('.tab-strip-tab[data-tab-id="agent-1"] .tab-stop')
      .click();
    expect(confirm).toHaveBeenCalled();
    expect(p._stopSubagent).toHaveBeenCalledWith(tab.subagent);
    confirm.mockRestore();
  });

  it('does nothing when the confirm is declined', async () => {
    const p = mountPanel();
    await withSubagent(p);
    p._stopSubagent = vi.fn();
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    p.shadowRoot
      .querySelector('.tab-strip-tab[data-tab-id="agent-1"] .tab-stop')
      .click();
    expect(p._stopSubagent).not.toHaveBeenCalled();
    confirm.mockRestore();
  });

  it('goes away once the subagent has settled', async () => {
    // Offering to stop something already over.
    const p = mountPanel();
    const { reqId } = await withSubagent(p);
    pushEvent('subagent-event', subagentEvent(reqId, {
      type: 'updated',
      status: 'killed',
      terminal: true,
    }));
    await settle(p);
    const btn = p.shadowRoot.querySelector(
      '.tab-strip-tab[data-tab-id="agent-1"]',
    );
    expect(btn.querySelector('.tab-stop')).toBeNull();
    // Stopped is neither a success nor a fault.
    expect(getLedState(p._tabs.get('agent-1'))).toBe('amber');
  });
});

// ---------------------------------------------------------------------------
// Lifetime
// ---------------------------------------------------------------------------

describe('subagent tabs leave when the turn does', () => {
  it('the next send starts from Main alone', async () => {
    const p = mountPanel();
    const { reqId } = await withSubagent(p);
    pushEvent('stream-complete', {
      requestId: reqId,
      result: { response: 'done', terminal_reason: 'completed' },
    });
    await settle(p);
    expect(p._tabs.size).toBe(2);
    await sendAndGetRequestId(p, 'again');
    await settle(p);
    expect(Array.from(p._tabs.keys())).toEqual(['main']);
  });

  it('a session change drops them, from whichever tab was active',
    async () => {
      const p = mountPanel();
      await withSubagent(p);
      p._activeTabId = 'agent-1';
      pushEvent('session-changed', {
        session_id: 'sess_new',
        messages: [{ role: 'user', content: 'new one' }],
      });
      await settle(p);
      expect(Array.from(p._tabs.keys())).toEqual(['main']);
      // Switched back first: every per-tab accessor reads the active tab, and
      // a missing key would throw on the next write.
      expect(p._activeTabId).toBe('main');
      expect(p.messages).toHaveLength(1);
    });
});

// ---------------------------------------------------------------------------
// Reconnect
// ---------------------------------------------------------------------------

describe('a refresh mid-fan-out', () => {
  it('rebuilds the tabs and their feeds from the snapshot', async () => {
    const p = mountPanel();
    await settle(p);
    resumeActiveStreams(p, [{
      request_id: 'req-1',
      session_id: 'sess-1',
      started_at: 1,
      blocks: [
        { block_id: 'req-1:b0', kind: 'text', seq: 0, content: 'Delegating.' },
        {
          block_id: 'req-1:b1',
          kind: 'text',
          seq: 0,
          content: 'reading the parser',
          agent_id: PARENT,
        },
      ],
      subagents: [{
        key: 'task-1',
        task_id: 'task-1',
        agent_id: 'agent-1',
        tool_use_id: PARENT,
        description: 'audit the parser',
        subagent_type: 'Explore',
        status: 'running',
        terminal: false,
      }],
    }]);
    await settle(p);
    expect(Array.from(p._tabs.keys())).toEqual(['main', 'agent-1']);
    const tab = p._tabs.get('agent-1');
    expect(tab.streaming).toBe(true);
    expect(getLedState(tab)).toBe('cyan');
    expect(tab.turnBlocks.blocks).toHaveLength(1);
    expect(tab.turnBlocks.blocks[0].content).toBe('reading the parser');
    expect(p._tabLabels.get('agent-1')).toBe('1 parser');
  });

  it('leaves Main active — a rebuilt tab is one nobody chose', async () => {
    // Observed once and never reproduced: the landing tab after a refresh was
    // a read-only feed, so the input surface was gone with no gesture that
    // asked for it. Asserted as a property rather than hunted as a cause.
    const p = mountPanel();
    await settle(p);
    resumeActiveStreams(p, [{
      request_id: 'req-1',
      session_id: 'sess-1',
      started_at: 1,
      blocks: [],
      subagents: [
        {
          key: 'task-1', task_id: 'task-1', agent_id: 'agent-1',
          tool_use_id: PARENT, description: 'audit the parser',
          subagent_type: 'Explore', terminal: false,
        },
        {
          key: 'task-2', task_id: 'task-2', agent_id: 'agent-2',
          tool_use_id: 'toolu_task2', description: 'check the tests',
          subagent_type: 'Explore', terminal: false,
        },
      ],
    }]);
    await settle(p);
    expect(Array.from(p._tabs.keys())).toEqual(['main', 'agent-1', 'agent-2']);
    expect(p._activeTabId).toBe('main');
    expect(p.shadowRoot.querySelectorAll('textarea').length).toBeGreaterThan(0);
  });

  it('a subagent that finished while we were away shows as finished',
    async () => {
      const p = mountPanel();
      await settle(p);
      resumeActiveStreams(p, [{
        request_id: 'req-1',
        started_at: 1,
        blocks: [],
        subagents: [{
          key: 'task-1',
          task_id: 'task-1',
          agent_id: 'agent-1',
          tool_use_id: PARENT,
          description: 'audit the parser',
          status: 'completed',
          terminal: true,
        }],
      }]);
      await settle(p);
      const tab = p._tabs.get('agent-1');
      expect(tab.streaming).toBe(false);
      expect(getLedState(tab)).toBe('green');
    });

  it('a snapshot with no subagents changes nothing', async () => {
    const p = mountPanel();
    await settle(p);
    resumeActiveStreams(p, [{
      request_id: 'req-1',
      started_at: 1,
      blocks: [{ block_id: 'req-1:b0', kind: 'text', seq: 0, content: 'Hi' }],
    }]);
    await settle(p);
    expect(Array.from(p._tabs.keys())).toEqual(['main']);
  });
});

// ---------------------------------------------------------------------------
// LED states and tooltips, directly
// ---------------------------------------------------------------------------

describe('subagentLedState', () => {
  it('is cyan while the subagent runs', () => {
    expect(subagentLedState({ terminal: false })).toBe('cyan');
  });

  it('maps the terminal statuses it knows', () => {
    expect(subagentLedState({ terminal: true, status: 'completed' }))
      .toBe('green');
    expect(subagentLedState({ terminal: true, status: 'failed' })).toBe('red');
    expect(subagentLedState({ terminal: true, status: 'stopped' }))
      .toBe('amber');
    expect(subagentLedState({ terminal: true, status: 'killed' }))
      .toBe('amber');
  });

  it('will not claim success for a status it has never heard of', () => {
    // The CLI's status vocabulary grows; a green dot is a claim.
    expect(subagentLedState({ terminal: true, status: 'transmogrified' }))
      .toBe('amber');
    expect(subagentLedState({ terminal: true, status: null })).toBe('amber');
  });
});

describe('subagentLedTooltip', () => {
  it('names the running tool while it runs', () => {
    const sub = { description: 'audit the parser', last_tool_name: 'Grep' };
    expect(subagentLedTooltip(sub, 'cyan'))
      .toBe('audit the parser: running — Grep');
  });

  it('reads the task usage the SDK reports, not the turn counters', () => {
    // `TaskUsage` shares no field names with the per-model token counters,
    // which is what made the row's token chip permanently blank.
    const sub = {
      description: 'audit the parser',
      usage: { total_tokens: 1200, tool_uses: 1 },
    };
    expect(subagentLedTooltip(sub, 'green'))
      .toBe('audit the parser: completed (1 tool, 1,200 tokens)');
  });

  it('drops a counter it has no number for rather than printing zero', () => {
    const sub = { description: 'audit the parser', usage: {} };
    expect(subagentLedTooltip(sub, 'green'))
      .toBe('audit the parser: completed');
  });

  it('says what it does not know', () => {
    const sub = { description: 'audit the parser', unknown: true };
    expect(subagentLedTooltip(sub, 'amber'))
      .toBe('audit the parser: status unknown at turn end');
  });
});

// ---------------------------------------------------------------------------
// Labels
// ---------------------------------------------------------------------------

describe('subagentKeyword', () => {
  it('takes the last word, where English puts the object', () => {
    expect(subagentKeyword('Count spec headings')).toBe('headings');
    expect(subagentKeyword('audit the parser')).toBe('parser');
  });

  it('reaches back when the last word identifies nothing', () => {
    expect(subagentKeyword('Count webapp test files')).toBe('test-files');
    expect(subagentKeyword('summarise the docs')).toBe('summarise-docs');
  });

  it('walks past the stopwords on the way back', () => {
    // `the-files` would spend a scarce character on nothing.
    expect(subagentKeyword('list all the files')).toBe('list-files');
    expect(subagentKeyword('check the tests')).toBe('check-tests');
  });

  it('stops at two words, or it is a description again', () => {
    expect(subagentKeyword('Count every webapp test file')).toBe('test-file');
  });

  it('keeps the basename of a path and drops the rest', () => {
    // The live fallback description is full of absolute paths, and a basename
    // is the only part of one that identifies anything.
    expect(subagentKeyword('Reading /home/me/repo/specs5/plan/delivery.md'))
      .toBe('delivery.md');
  });

  it('truncates rather than pushing the next tab off the strip', () => {
    expect(subagentKeyword('check internationalisation')).toBe('international…');
  });

  it('returns nothing for text with no word in it', () => {
    expect(subagentKeyword('')).toBe('');
    expect(subagentKeyword('   ///  ')).toBe('');
    expect(subagentKeyword(null)).toBe('');
  });
});

describe('the label a tab keeps', () => {
  /** The parent `Task` card, as the engine sends it. */
  function taskCard(reqId, input) {
    return {
      requestId: reqId,
      data: { tool_use_id: PARENT, name: 'Task', input },
    };
  }

  it('prefers what the Task asked for over what the subagent is doing',
    async () => {
      const p = mountPanel();
      const reqId = await sendAndGetRequestId(p);
      pushEvent('tool-use', taskCard(reqId, {
        description: 'Count spec headings',
        subagent_type: 'Explore',
      }));
      await settle(p);
      // The SDK's live description is an *activity* string, not the task.
      pushEvent('subagent-event', subagentEvent(reqId, {
        description: 'Running grep -c "^## " /home/me/repo/specs5/plan/x.md',
        task_type: 'local_agent',
      }));
      await settle(p);
      expect(p._tabLabels.get('agent-1')).toBe('1 headings');
      const btn = p.shadowRoot.querySelector(
        '.tab-strip-tab[data-tab-id="agent-1"]',
      );
      expect(btn.title).toBe(
        '1 · Explore — Count spec headings — subagent feed (read-only)',
      );
    });

  it('does not follow the activity string as it changes', async () => {
    // The bug this replaced: a settled tab was labelled with whatever the
    // subagent happened to be doing when its last event landed.
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('tool-use', taskCard(reqId, {
      description: 'Count spec headings',
      subagent_type: 'Explore',
    }));
    await settle(p);
    pushEvent('subagent-event', subagentEvent(reqId, {
      description: 'Running grep',
    }));
    await settle(p);
    pushEvent('subagent-event', subagentEvent(reqId, {
      type: 'progress',
      description: 'Reading /home/me/repo/specs5/plan/delivery.md',
      last_tool_name: 'Read',
    }));
    pushEvent('subagent-event', subagentEvent(reqId, {
      type: 'notification',
      status: 'completed',
      terminal: true,
      description: 'Reading /home/me/repo/webapp/src/chat-panel/tabs.js',
    }));
    await settle(p);
    expect(p._tabLabels.get('agent-1')).toBe('1 headings');
    expect(p._tabs.get('agent-1').messages[0].content)
      .toBe('🤖 Explore — Count spec headings');
  });

  it('upgrades an event-sourced label when the card arrives late', async () => {
    // A reconnect can replay the row before the card that spawned it.
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('subagent-event', subagentEvent(reqId, {
      description: 'Running grep over the specs',
    }));
    await settle(p);
    expect(p._tabLabels.get('agent-1')).toBe('1 specs');
    pushEvent('tool-use', taskCard(reqId, {
      description: 'Count spec headings',
      subagent_type: 'Explore',
    }));
    pushEvent('subagent-event', subagentEvent(reqId, { type: 'progress' }));
    await settle(p);
    expect(p._tabLabels.get('agent-1')).toBe('1 headings');
  });

  it('falls back to the id when there is nothing to read', async () => {
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('subagent-event', subagentEvent(reqId, {
      description: '',
      subagent_type: '',
      agent_id: '',
      task_id: 'task-1',
    }));
    await settle(p);
    expect(p._tabLabels.get('task-1')).toBe('1 task-1');
  });
});
