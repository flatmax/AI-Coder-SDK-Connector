// Tests for a consultation's strip tab: who creates it, and when.
//
// Every other subagent gets a tab the moment its first event lands, because a
// delegated `Task` runs for minutes and its work is unreadable interleaved
// with the turn that spawned it. A consultation is not that. It is one
// question and one answer, it renders inline on the card that asked it, and
// giving every one of them a tab filled the strip with feeds competing for
// attention with the stream the reader was already in.
//
// So the tab is opt-in: nothing makes one, the card's name button does. The
// tests below pin both halves — that it does not appear on its own, and that
// it appears complete when asked for, including the part of the answer that
// arrived before the click.
//
// Governing spec: specs5/5-webapp/subagent-browser.md, decision AG-31.

import { describe, expect, it, vi } from 'vitest';

import { rehydrateSubagentTabs, subagentTabs } from './subagent-tabs.js';
import {
  mountPanel,
  publishFakeRpc,
  pushEvent,
  settle,
} from './test-helpers.js';

// The `second_opinion` call that spawned the consultation. Blocks written
// inside it carry this as their `agent_id` — a consultation's own `agent_id`
// is minted and has no session behind it.
const PARENT = 'toolu_consult';

async function sendAndGetRequestId(panel, message = 'hi') {
  const started = vi.fn().mockResolvedValue({ status: 'started' });
  publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
  await settle(panel);
  panel._input = message;
  await panel._send();
  return started.mock.calls[0][0];
}

function consultEvent(reqId, over = {}) {
  return {
    requestId: reqId,
    data: {
      type: 'started',
      task_id: 'consult-1',
      agent_id: 'consult-1',
      tool_use_id: PARENT,
      description: 'is this design sound?',
      task_type: 'consultation',
      has_transcript: false,
      terminal: false,
      ...over,
    },
  };
}

function delegateEvent(reqId, over = {}) {
  return {
    requestId: reqId,
    data: {
      type: 'started',
      task_id: 'task-1',
      agent_id: 'agent-1',
      tool_use_id: 'toolu_task',
      description: 'audit the parser',
      subagent_type: 'Explore',
      terminal: false,
      ...over,
    },
  };
}

/** The `second_opinion` card the row nests under, as the engine sends it. */
function consultCard(reqId, toolUseId = PARENT) {
  return {
    requestId: reqId,
    data: {
      tool_use_id: toolUseId,
      name: 'mcp__aic-dc-antigravity__second_opinion',
      input: { question: 'is this design sound?' },
    },
  };
}

function feedChunk(reqId, content, seq = 0, agentId = PARENT) {
  return {
    requestId: reqId,
    chunk: {
      block_id: `${reqId}:b${seq}`,
      seq,
      content,
      done: false,
      agent_id: agentId,
    },
  };
}

/** Click the row's name button, which is the only way to a consultation tab. */
function clickTheRow(panel) {
  const button = panel.shadowRoot.querySelector('.subagent-desc-button');
  expect(button).toBeTruthy();
  button.click();
}

function contentsOf(tab) {
  return (tab?.turnBlocks?.blocks || []).map((b) => b.content);
}

// ---------------------------------------------------------------------------

describe('a consultation does not get a tab on its own', () => {
  it('runs with no tab at all', async () => {
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('tool-use', consultCard(reqId));
    pushEvent('subagent-event', consultEvent(reqId));
    await settle(p);
    expect(subagentTabs(p)).toHaveLength(0);
    expect(p._activeTabId).toBe('main');
  });

  it('still draws its row, which is where the answer goes', async () => {
    // The row is not a pointer to the tab — it is the surface. Refusing the
    // tab must not cost the repaint that draws the row itself, which is why
    // `onSubagentEvent` marks the blocks dirty before it checks whether a
    // tab was made.
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('tool-use', consultCard(reqId));
    pushEvent('subagent-event', consultEvent(reqId));
    await settle(p);
    const row = p.shadowRoot.querySelector('.subagent-row');
    expect(row).toBeTruthy();
    expect(row.textContent).toContain('is this design sound?');
  });

  it('leaves a delegated subagent its tab, which is not opt-in', async () => {
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('tool-use', {
      requestId: reqId,
      data: { tool_use_id: 'toolu_task', name: 'Task', input: {} },
    });
    pushEvent('subagent-event', delegateEvent(reqId));
    await settle(p);
    expect(subagentTabs(p)).toHaveLength(1);
  });
});

describe('the card opens the tab when asked', () => {
  it('makes one on the click and goes to it', async () => {
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('tool-use', consultCard(reqId));
    pushEvent('subagent-event', consultEvent(reqId));
    await settle(p);
    clickTheRow(p);
    await settle(p);
    const open = subagentTabs(p);
    expect(open).toHaveLength(1);
    expect(p._activeTabId).toBe(open[0].tabId);
    // A live consultation gets a *live* tab, not a snapshot: it has not
    // finished producing, and an archived projection would freeze it at
    // whatever it had said when the click landed.
    expect(open[0].tabId.startsWith('historical:')).toBe(false);
    expect(open[0].tab.streaming).toBe(true);
    expect(open[0].tab.readOnly).toBe(true);
  });

  it('backfills what it already said', async () => {
    // The reason the click cannot simply create an empty tab and wait: a
    // consultation is opened *because* it has said something worth reading,
    // so the interesting blocks are always the ones that predate the click.
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('tool-use', consultCard(reqId));
    pushEvent('subagent-event', consultEvent(reqId));
    pushEvent('stream-chunk', feedChunk(reqId, 'The design holds.', 0));
    await settle(p);
    clickTheRow(p);
    await settle(p);
    expect(contentsOf(subagentTabs(p)[0].tab)).toEqual(['The design holds.']);
  });

  it('keeps streaming into it after the click', async () => {
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('tool-use', consultCard(reqId));
    pushEvent('subagent-event', consultEvent(reqId));
    pushEvent('stream-chunk', feedChunk(reqId, 'First, ', 0));
    await settle(p);
    clickTheRow(p);
    await settle(p);
    pushEvent('stream-chunk', feedChunk(reqId, 'and second.', 1));
    await settle(p);
    expect(contentsOf(subagentTabs(p)[0].tab)).toEqual([
      'First, ',
      'and second.',
    ]);
  });

  it('does not double up a block it backfilled and then streamed', async () => {
    // `mirrorSubagentBlocks` is idempotent on `block_id`, which is what makes
    // the catch-up safe to run on a tab that is about to receive the same
    // block through the ordinary path.
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('tool-use', consultCard(reqId));
    pushEvent('subagent-event', consultEvent(reqId));
    pushEvent('stream-chunk', feedChunk(reqId, 'Partial', 0));
    await settle(p);
    clickTheRow(p);
    await settle(p);
    pushEvent('stream-chunk', {
      requestId: reqId,
      chunk: {
        block_id: `${reqId}:b0`,
        seq: 1,
        content: ' answer',
        done: false,
        agent_id: PARENT,
      },
    });
    await settle(p);
    // One block, not two: the same record arrived by both routes.
    expect(subagentTabs(p)[0].tab.turnBlocks.blocks).toHaveLength(1);
  });

  it('goes back to the same tab on a second click', async () => {
    // A click that silently did nothing because the first one worked reads
    // as a broken button.
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('tool-use', consultCard(reqId));
    pushEvent('subagent-event', consultEvent(reqId));
    await settle(p);
    clickTheRow(p);
    await settle(p);
    const tabId = p._activeTabId;
    p._activeTabId = 'main';
    await settle(p);
    clickTheRow(p);
    await settle(p);
    expect(subagentTabs(p)).toHaveLength(1);
    expect(p._activeTabId).toBe(tabId);
  });

  it('returns to the same tab after it finishes, not a second one', async () => {
    // The boundary a reviewer expected to be a duplicate-tab bug: click while
    // it streams, let it finish, click again. The handler looks for an open
    // tab by agent id *before* it decides what kind to build, which is the
    // check that stops one consultation ending up with a live feed and an
    // archived snapshot of the same answer side by side.
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('tool-use', consultCard(reqId));
    pushEvent('subagent-event', consultEvent(reqId));
    await settle(p);
    clickTheRow(p);
    await settle(p);
    const tabId = p._activeTabId;

    pushEvent('subagent-event', consultEvent(reqId, {
      type: 'completed',
      status: 'completed',
      terminal: true,
    }));
    p._activeTabId = 'main';
    await settle(p);
    clickTheRow(p);
    await settle(p);
    expect(subagentTabs(p)).toHaveLength(1);
    expect(p._tabs.has('historical:consult-1')).toBe(false);
    expect(p._activeTabId).toBe(tabId);
  });

  it('tracks its later events like any other tab', async () => {
    // Creation is gated, updating is not: once the tab exists it must follow
    // the consultation to its end, or the LED would stay cyan over finished
    // work.
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('tool-use', consultCard(reqId));
    pushEvent('subagent-event', consultEvent(reqId));
    await settle(p);
    clickTheRow(p);
    await settle(p);
    pushEvent('subagent-event', consultEvent(reqId, {
      type: 'completed',
      status: 'completed',
      terminal: true,
    }));
    await settle(p);
    const [{ tab }] = subagentTabs(p);
    expect(tab.subagent.terminal).toBe(true);
    expect(tab.streaming).toBe(false);
  });
});

describe('one tab, wherever its blocks have got to', () => {
  it('gives a real tab to one that finished inside the turn', async () => {
    // `row.terminal` is not the discriminator, and pinning that is the point
    // of this test: a consultation is one question and one answer, so it is
    // usually over well before the turn that asked it. Keying on it would
    // mean the kind of tab you get depends on whether you clicked before or
    // after an event you cannot see.
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('tool-use', consultCard(reqId));
    pushEvent('subagent-event', consultEvent(reqId));
    pushEvent('stream-chunk', feedChunk(reqId, 'It holds.', 0));
    pushEvent('subagent-event', consultEvent(reqId, {
      type: 'completed',
      status: 'completed',
      terminal: true,
    }));
    await settle(p);
    expect(subagentTabs(p)).toHaveLength(0);

    clickTheRow(p);
    await settle(p);
    const open = subagentTabs(p);
    expect(open).toHaveLength(1);
    expect(open[0].tabId.startsWith('historical:')).toBe(false);
    // Settled, not spinning — which is what a live tab becomes when it ends.
    expect(open[0].tab.streaming).toBe(false);
    expect(contentsOf(open[0].tab)).toEqual(['It holds.']);
  });

  it('holds the answer when the turn ends under an already-open tab',
    async () => {
      // The other order, and the one a reviewer thought was broken: the tab
      // is opened *while* the consultation streams, and the turn then ends
      // under it. Nothing re-opens the tab at that point, so if the ending
      // did not carry the mirror into a settled message the tab would empty
      // out when its owner's block list did. It does not: the mirror is the
      // tab's own array, and `settleSubagentTab` — the same one every
      // delegated subagent settles through — hands it to a feed message.
      const p = mountPanel();
      const reqId = await sendAndGetRequestId(p);
      pushEvent('tool-use', consultCard(reqId));
      pushEvent('subagent-event', consultEvent(reqId));
      pushEvent('stream-chunk', feedChunk(reqId, 'Half an ans', 0));
      await settle(p);
      clickTheRow(p);
      await settle(p);
      const opened = p._activeTabId;

      pushEvent('stream-chunk', feedChunk(reqId, ', and the rest.', 1));
      pushEvent('subagent-event', consultEvent(reqId, {
        type: 'completed',
        status: 'completed',
        terminal: true,
      }));
      pushEvent('stream-complete', {
        requestId: reqId,
        result: { response: 'done', terminal_reason: 'completed' },
      });
      await settle(p);

      const tab = p._tabs.get(opened);
      expect(tab.streaming).toBe(false);
      expect(contentsOf(tab)).toEqual(['Half an ans', ', and the rest.']);
      // Not merely mirrored: settled into the feed the renderer draws from
      // once the streaming card is gone.
      expect(tab.messages.at(-1).blocks.map((b) => b.content))
        .toEqual(['Half an ans', ', and the rest.']);

      // And clicking the card again lands back on that same tab rather than
      // minting a second one beside it.
      p._activeTabId = 'main';
      await settle(p);
      clickTheRow(p);
      await settle(p);
      expect(subagentTabs(p)).toHaveLength(1);
      expect(p._activeTabId).toBe(opened);
      expect(contentsOf(p._tabs.get(opened)))
        .toEqual(['Half an ans', ', and the rest.']);
    });

  it('reads a past turn\'s answer out of the message it settled into',
    async () => {
      // The same tab, from the other source. A turn empties its block list
      // at its end, so a consultation the user comes back to a turn later
      // has its record in a settled message — but it is the same
      // consultation and gets the same tab, under the same id. Keying the
      // two cases differently is what made the entity you got depend on a
      // boundary nobody can see.
      const p = mountPanel();
      await settle(p);
      p._tabs.get('main').messages = [{
        role: 'assistant',
        blocks: [
          { block_id: 'c0', kind: 'text', content: 'It held.',
            agent_id: PARENT },
        ],
      }];
      p._onViewSubagentsRequested(new CustomEvent('view-subagents-requested', {
        detail: {
          agents: [{
            agent_id: 'consult-1',
            label: 'Antigravity: is this design sound?',
            has_transcript: false,
            tool_use_id: PARENT,
            row: { key: 'consult-1', agent_id: 'consult-1',
                   tool_use_id: PARENT, task_type: 'consultation',
                   terminal: true },
          }],
        },
      }));
      await settle(p);
      const open = subagentTabs(p);
      expect(open).toHaveLength(1);
      expect(open[0].tabId).toBe('consultation:consult-1');
      expect(p._tabs.has('historical:consult-1')).toBe(false);
      expect(open[0].tab.messages[0].blocks.map((b) => b.block_id))
        .toEqual(['c0']);
      expect(open[0].tab.readOnly).toBe(true);
    });
});

describe('a tab the user opened survives the strip regrowing', () => {
  it('is left alone by a reconnect, which opens none of its own', async () => {
    // A reconnect mid-turn replays the engine's subagent snapshot through
    // `syncSubagentTab` to rebuild the strip. That function creates nothing
    // for a consultation, so the two in this snapshot stay closed — and the
    // one the user opened is not re-created either, because it was never
    // destroyed. Nothing has to remember the asking: the tab *is* the record
    // of it.
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('tool-use', consultCard(reqId));
    pushEvent('tool-use', consultCard(reqId, 'toolu_other'));
    pushEvent('subagent-event', consultEvent(reqId));
    pushEvent('subagent-event', consultEvent(reqId, {
      task_id: 'consult-2',
      agent_id: 'consult-2',
      tool_use_id: 'toolu_other',
      description: 'and this one?',
    }));
    await settle(p);
    clickTheRow(p);
    await settle(p);
    const wanted = p._activeTabId;

    const owner = p._tabs.get('main');
    rehydrateSubagentTabs(p, reqId, [
      { key: 'consult-1', agent_id: 'consult-1', tool_use_id: PARENT,
        task_type: 'consultation', description: 'is this design sound?',
        status: 'running' },
      { key: 'consult-2', agent_id: 'consult-2', tool_use_id: 'toolu_other',
        task_type: 'consultation', description: 'and this one?' },
    ], owner);

    const open = subagentTabs(p);
    expect(open.map(({ tabId }) => tabId)).toEqual([wanted]);
    // And it took the update the snapshot carried, like any other tab.
    expect(open[0].tab.subagent.status).toBe('running');
  });

  it('is still there after the send, because that is when it is read',
    async () => {
      // The one exception to "a new turn starts from Main alone". The reason
      // to open a consultation's tab, given its answer already renders on the
      // card, is to keep the second opinion beside the composer while writing
      // the reply to it — so sweeping it at the send takes the reference away
      // at the exact keystroke it exists for. A delegation's tab still goes:
      // its transcript is on disk and "View subagents" brings it back.
      const p = mountPanel();
      const reqId = await sendAndGetRequestId(p);
      pushEvent('tool-use', consultCard(reqId));
      pushEvent('subagent-event', consultEvent(reqId));
      pushEvent('tool-use', {
        requestId: reqId,
        data: { tool_use_id: 'toolu_task', name: 'Task', input: {} },
      });
      pushEvent('subagent-event', delegateEvent(reqId));
      await settle(p);
      clickTheRow(p);
      await settle(p);
      const wanted = p._activeTabId;
      expect(subagentTabs(p)).toHaveLength(2);

      pushEvent('stream-complete', {
        requestId: reqId,
        result: { response: 'done', terminal_reason: 'completed' },
      });
      p._activeTabId = 'main';
      await settle(p);
      const next = await sendAndGetRequestId(p, 'again');
      await settle(p);
      expect(subagentTabs(p).map(({ tabId }) => tabId)).toEqual([wanted]);

      // And it does not renumber what follows it. The ordinal counts
      // delegations, so the next turn's first one is 1 with the survivor
      // still in the strip beside it.
      pushEvent('tool-use', {
        requestId: next,
        data: { tool_use_id: 'toolu_task2', name: 'Task', input: {} },
      });
      pushEvent('subagent-event', delegateEvent(next, {
        task_id: 'task-2', agent_id: 'agent-2', tool_use_id: 'toolu_task2',
        description: 'audit the lexer',
      }));
      await settle(p);
      const fresh = subagentTabs(p).find(({ tabId }) => tabId !== wanted);
      expect(fresh.tab.subagent.ordinal).toBe(1);
      expect(p._tabs.get(wanted).subagent.ordinal).toBe(0);
    });

  it('goes when the session does, which its blocks belong to', async () => {
    // The other half of the exemption. A consultation tab holds blocks from
    // a transcript that a session change has just replaced; keeping it would
    // leave one turn's second opinion open over somebody else's conversation.
    const p = mountPanel();
    const reqId = await sendAndGetRequestId(p);
    pushEvent('tool-use', consultCard(reqId));
    pushEvent('subagent-event', consultEvent(reqId));
    await settle(p);
    clickTheRow(p);
    await settle(p);
    expect(subagentTabs(p)).toHaveLength(1);

    window.dispatchEvent(new CustomEvent('session-changed', {
      detail: { messages: [] },
    }));
    await settle(p);
    expect(subagentTabs(p)).toHaveLength(0);
    expect(p._activeTabId).toBe('main');
  });
});
