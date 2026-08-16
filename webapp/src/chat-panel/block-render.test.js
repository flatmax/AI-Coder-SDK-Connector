// Tests for block-render.js — what a Claude Code turn looks like.
//
// blocks.test.js pins what a turn *is*. This file pins what the user reads off
// it, in two styles because the module has two halves:
//
//   - The pure helpers are called directly. They hold the judgements — which
//     badge a terminal reason earns, whether a card opens itself, whether there
//     is a cost worth showing — and none of them need a DOM.
//   - The templates are rendered into a detached container against a stub
//     panel, so a tool card's markup can be checked without a tab, an engine,
//     or a turn in flight.
//
// The properties worth failing over:
//
//   1. **An unrecognised terminal reason is not a success.** The engine's
//      vocabulary is open-ended; `✓ completed` on a turn that actually refused
//      is the one badge worse than no badge at all.
//   2. **Cost is null, not zero.** Subscription billing reports no cost, and
//      `$0.00` reads as "that turn was free" (risks.md § R-6).
//   3. **A failed or denied call opens itself — and the first click still
//      closes it.** Auto-expansion that swallows the first click reads as a
//      broken button.
//   4. **A denial replaces the input** instead of sitting beside it. The call
//      never ran, so its input is a proposal, and the reason is the record.
//   5. **Truncation names a size, not a button.** The untruncated text never
//      leaves the server, so "show all" would re-show the same preview.
//   6. **A subagent's blocks nest under its row, and a row with no card is
//      still shown.** A subagent running invisibly is worse than one in the
//      wrong place.

import { render } from 'lit';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  blockExpanded,
  compactionSummary,
  diffSegments,
  formatBytes,
  formatDuration,
  formatTokens,
  groupBlocksByScope,
  isDiffTool,
  renderBlock,
  renderFileChip,
  renderSubagentRow,
  renderTerminalBadge,
  renderTextBlock,
  renderThinkingBlock,
  renderTodoList,
  renderToolCard,
  renderTurnBlocks,
  renderTurnFooter,
  terminalBadge,
  toggleBlock,
  toolLabel,
} from './block-render.js';

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

const hosts = [];

/** Render a template into a live container so `.click()` and CSS both work. */
function draw(template) {
  const host = document.createElement('div');
  document.body.appendChild(host);
  hosts.push(host);
  render(template, host);
  return host;
}

afterEach(() => {
  while (hosts.length) hosts.pop().remove();
});

/**
 * The parts of a ChatPanel these renderers actually touch. Deliberately not a
 * real panel: a template that quietly grows a dependency on tab state should
 * fail here rather than pass on a mount that supplies everything.
 */
function stubPanel(props = {}) {
  return {
    _blockExpansion: new Map(),
    repoFiles: [],
    requestUpdate: vi.fn(),
    _stopSubagent: vi.fn(),
    ...props,
  };
}

/** Collapse the whitespace Lit's template literals leave in textContent. */
const text = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);

function toolBlock({ tool = {}, ...rest } = {}) {
  return {
    kind: 'tool',
    block_id: 'toolu_01',
    tool: { name: 'Bash', input: { command: 'ls -la' }, input_summary: 'ls -la', ...tool },
    ...rest,
  };
}

const textBlock = (over = {}) => ({
  kind: 'text',
  block_id: 'req-1:b0',
  content: 'hello',
  ...over,
});

// ---------------------------------------------------------------------------
// Terminal-reason badge
// ---------------------------------------------------------------------------

describe('terminalBadge', () => {
  it('draws nothing when the engine reported no reason', () => {
    // Local slash-command results and older CLIs report nothing. A badge here
    // would have to guess, and the only available guess is "success".
    for (const reason of [null, undefined, '', 0, 42, {}]) {
      expect(terminalBadge(reason)).toBeNull();
    }
  });

  it('puts the one clean finish in the footer', () => {
    expect(terminalBadge('completed')).toEqual({
      label: 'completed',
      severity: 'natural',
      placement: 'footer',
    });
  });

  it('reads an interruption as information, at the top of the card', () => {
    // Top of the card because an interrupted turn may have left a half-applied
    // edit on disk — the user has to see it before reading the body.
    expect(terminalBadge('aborted_streaming')).toEqual({
      label: 'interrupted',
      severity: 'neutral',
      placement: 'header',
    });
    expect(terminalBadge('aborted_tools')).toEqual({
      label: 'interrupted mid-tool',
      severity: 'neutral',
      placement: 'header',
    });
  });

  it('labels the failures it knows', () => {
    const labels = ['max_turns', 'refusal', 'engine_error', 'session_lost']
      .map((r) => terminalBadge(r));
    expect(labels.map((b) => b.label)).toEqual([
      'turn limit reached',
      'refused',
      'engine error',
      'session lost',
    ]);
    for (const badge of labels) {
      expect(badge.severity).toBe('error');
      expect(badge.placement).toBe('header');
    }
  });

  it('treats a reason it has never seen as a failure, not a success', () => {
    // The property this whole family exists for. A newer CLI inventing
    // `context_exhausted` must not have it rendered as a clean finish.
    const badge = terminalBadge('context_exhausted_mid_tool');
    expect(badge.severity).toBe('error');
    expect(badge.placement).toBe('header');
    // Underscores are wire syntax, not English.
    expect(badge.label).toBe('context exhausted mid tool');
  });
});

describe('renderTerminalBadge', () => {
  it('renders nothing at all for no reason', () => {
    const host = draw(renderTerminalBadge(null));
    expect(host.querySelector('.finish-reason-badge')).toBeNull();
  });

  it('ticks a clean finish and carries its severity as a class', () => {
    const host = draw(renderTerminalBadge('completed'));
    const badge = host.querySelector('.finish-reason-badge');
    expect(badge.classList.contains('severity-natural')).toBe(true);
    expect(text(badge)).toBe('✓ completed');
  });

  it('does not tick anything else', () => {
    const host = draw(renderTerminalBadge('refusal'));
    const badge = host.querySelector('.finish-reason-badge');
    expect(text(badge)).toBe('refused');
    expect(badge.classList.contains('severity-error')).toBe(true);
  });

  it('keeps the raw reason in the tooltip so an unmapped value is diagnosable', () => {
    const host = draw(renderTerminalBadge('context_exhausted'));
    expect(
      host.querySelector('.finish-reason-badge').getAttribute('title'),
    ).toBe('terminal_reason: context_exhausted');
  });
});

// ---------------------------------------------------------------------------
// Expansion state
// ---------------------------------------------------------------------------

describe('blockExpanded', () => {
  it('starts everything collapsed', () => {
    const panel = stubPanel();
    expect(blockExpanded(panel, textBlock())).toBe(false);
    expect(blockExpanded(panel, { kind: 'thinking', block_id: 'b1' })).toBe(false);
    expect(blockExpanded(panel, toolBlock({ result: { status: 'ok' } }))).toBe(false);
  });

  it('opens a call that failed or was denied', () => {
    // Driven by the status flag, never by sniffing the result text.
    const panel = stubPanel();
    expect(blockExpanded(panel, toolBlock({ result: { status: 'error' } }))).toBe(true);
    expect(blockExpanded(panel, toolBlock({ denial: { action: 'deny' } }))).toBe(true);
  });

  it('leaves a call waiting on permission collapsed', () => {
    // The dialog is where the user reads it; the card would be a second copy.
    expect(blockExpanded(stubPanel(), toolBlock({ awaiting: true }))).toBe(false);
  });

  it('lets a click beat the auto-expansion, in both directions', () => {
    const panel = stubPanel();
    const failed = toolBlock({ result: { status: 'error' } });
    panel._blockExpansion.set(failed.block_id, false);
    expect(blockExpanded(panel, failed)).toBe(false);

    const quiet = textBlock();
    panel._blockExpansion.set(quiet.block_id, true);
    expect(blockExpanded(panel, quiet)).toBe(true);
  });

  it('survives a panel with no expansion map and a missing block', () => {
    expect(blockExpanded(undefined, textBlock())).toBe(false);
    expect(blockExpanded({}, textBlock())).toBe(false);
    expect(blockExpanded(stubPanel(), undefined)).toBe(false);
  });
});

describe('toggleBlock', () => {
  it('records the flip and repaints', () => {
    const panel = stubPanel();
    const block = textBlock();
    toggleBlock(panel, block);
    expect(panel._blockExpansion.get(block.block_id)).toBe(true);
    expect(panel.requestUpdate).toHaveBeenCalledOnce();
    toggleBlock(panel, block);
    expect(panel._blockExpansion.get(block.block_id)).toBe(false);
  });

  it('closes an auto-expanded error card on the first click', () => {
    // Reading the stored state instead of the effective one would make the
    // first click a no-op, which reads as a broken button.
    const panel = stubPanel();
    const failed = toolBlock({ result: { status: 'error' } });
    toggleBlock(panel, failed);
    expect(blockExpanded(panel, failed)).toBe(false);
  });

  it('creates the map on a panel that has none', () => {
    const panel = { requestUpdate: vi.fn() };
    toggleBlock(panel, textBlock());
    expect(panel._blockExpansion.get('req-1:b0')).toBe(true);
  });

  it('does nothing without a panel or a block id', () => {
    const panel = stubPanel();
    expect(() => toggleBlock(null, textBlock())).not.toThrow();
    toggleBlock(panel, { kind: 'text' });
    expect(panel._blockExpansion.size).toBe(0);
    expect(panel.requestUpdate).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

describe('formatDuration', () => {
  it('is empty rather than "NaNms" for a missing duration', () => {
    for (const value of [null, undefined, NaN, Infinity, -1, '500']) {
      expect(formatDuration(value)).toBe('');
    }
  });

  it('reads sub-second in whole milliseconds', () => {
    expect(formatDuration(0)).toBe('0ms');
    expect(formatDuration(12.4)).toBe('12ms');
    expect(formatDuration(999)).toBe('999ms');
  });

  it('reads seconds to one decimal', () => {
    expect(formatDuration(1000)).toBe('1.0s');
    expect(formatDuration(1540)).toBe('1.5s');
    expect(formatDuration(59_900)).toBe('59.9s');
  });

  it('reads minutes and seconds past a minute', () => {
    expect(formatDuration(60_000)).toBe('1m 0s');
    expect(formatDuration(90_000)).toBe('1m 30s');
    expect(formatDuration(3_725_000)).toBe('62m 5s');
  });
});

describe('formatBytes', () => {
  it('is empty for a size the engine did not report', () => {
    for (const value of [null, undefined, NaN, -1, '2048']) {
      expect(formatBytes(value)).toBe('');
    }
  });

  it('scales at the usual boundaries', () => {
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(1023)).toBe('1023 B');
    expect(formatBytes(1024)).toBe('1.0 KB');
    expect(formatBytes(2560)).toBe('2.5 KB');
    expect(formatBytes(1024 * 1024)).toBe('1.0 MB');
    expect(formatBytes(5.5 * 1024 * 1024)).toBe('5.5 MB');
  });
});

describe('formatTokens', () => {
  it('is empty for a count the engine did not report', () => {
    for (const value of [null, undefined, NaN, -5, '1000']) {
      expect(formatTokens(value)).toBe('');
    }
  });

  it('shows small counts exactly and large ones abbreviated', () => {
    expect(formatTokens(0)).toBe('0');
    expect(formatTokens(999)).toBe('999');
    expect(formatTokens(1000)).toBe('1.0k');
    expect(formatTokens(168_200)).toBe('168.2k');
    expect(formatTokens(1_000_000)).toBe('1.00M');
    expect(formatTokens(2_540_000)).toBe('2.54M');
  });
});

// `totalTokens`, `usageLines` and `formatCost` moved to ../turn-cost.js, which
// the usage HUD reads too — see turn-cost.test.js for their coverage. The
// footer's use of them is still exercised below.

// ---------------------------------------------------------------------------
// Compaction summary
// ---------------------------------------------------------------------------

describe('compactionSummary', () => {
  it('still says compaction happened with nothing on the wire', () => {
    // The subtype falls through to an untyped SystemMessage in the SDK, so
    // every field is optional and a renamed one must not produce
    // "undefined → undefined".
    for (const input of [undefined, null, {}, 'compacted']) {
      expect(compactionSummary(input)).toEqual({
        pre: null,
        post: null,
        trigger: null,
        counts: '',
        text: 'Context compacted',
      });
    }
  });

  it('reads both counts as a transition', () => {
    const summary = compactionSummary({
      pre_tokens: 168_200,
      post_tokens: 21_400,
      trigger: 'auto',
    });
    expect(summary.counts).toBe('168.2k → 21.4k tokens');
    expect(summary.text).toBe('Context compacted (automatic) — 168.2k → 21.4k tokens');
  });

  it('reads one count as a direction', () => {
    expect(compactionSummary({ pre_tokens: 5000 }).counts).toBe('from 5.0k tokens');
    expect(compactionSummary({ post_tokens: 400 }).counts).toBe('to 400 tokens');
  });

  it('drops a count that is not a number', () => {
    const summary = compactionSummary({ pre_tokens: '168200', post_tokens: -3 });
    expect(summary.pre).toBeNull();
    expect(summary.post).toBeNull();
    expect(summary.counts).toBe('');
  });

  it('passes an unrecognised trigger through verbatim', () => {
    expect(compactionSummary({ trigger: 'microcompact' }).trigger).toBe('microcompact');
    expect(compactionSummary({ trigger: 'manual' }).trigger).toBe('manual');
    expect(compactionSummary({ trigger: '' }).trigger).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Tool naming and diffs
// ---------------------------------------------------------------------------

describe('toolLabel', () => {
  it('shows a built-in tool by its own name', () => {
    expect(toolLabel({ name: 'Bash' })).toBe('Bash');
  });

  it('strips the mcp prefix and server, which get their own chip', () => {
    expect(toolLabel({ name: 'mcp__chrome-devtools__take_snapshot' }))
      .toBe('take_snapshot');
  });

  it('keeps a namespaced tool name whole below the server', () => {
    expect(toolLabel({ name: 'mcp__planner__todo__write' })).toBe('todo__write');
  });

  it('leaves a malformed mcp name alone rather than mangling it', () => {
    expect(toolLabel({ name: 'mcp__lonely' })).toBe('mcp__lonely');
  });

  it('is empty for no card at all', () => {
    expect(toolLabel(null)).toBe('');
    expect(toolLabel({})).toBe('');
    expect(toolLabel({ name: 42 })).toBe('');
  });
});

describe('isDiffTool', () => {
  it('knows the four tools whose input is a diff in disguise', () => {
    for (const name of ['Edit', 'MultiEdit', 'Write', 'NotebookEdit']) {
      expect(isDiffTool(name)).toBe(true);
    }
  });

  it('treats an MCP re-export the same as the built-in', () => {
    expect(isDiffTool('mcp__filesystem__Edit')).toBe(true);
  });

  it('says no to everything else', () => {
    for (const name of ['Bash', 'Read', 'Editor', '', null, undefined, 7]) {
      expect(isDiffTool(name)).toBe(false);
    }
  });
});

describe('diffSegments', () => {
  it('is empty for a tool that is not edit-shaped', () => {
    expect(diffSegments(toolBlock())).toEqual([]);
    expect(diffSegments(textBlock())).toEqual([]);
    expect(diffSegments(null)).toEqual([]);
    expect(diffSegments(toolBlock({ tool: { name: 'Edit', input: 'src/a.js' } }))).toEqual([]);
  });

  it('reads an Edit as one old/new pair', () => {
    expect(diffSegments(toolBlock({
      tool: { name: 'Edit', input: { file_path: 'a.js', old_string: 'a', new_string: 'b' } },
    }))).toEqual([{ oldText: 'a', newText: 'b' }]);
  });

  it('renders a one-sided Edit against an empty side', () => {
    expect(diffSegments(toolBlock({
      tool: { name: 'Edit', input: { new_string: 'added' } },
    }))).toEqual([{ oldText: '', newText: 'added' }]);
  });

  it('gives up on an Edit carrying neither side', () => {
    expect(diffSegments(toolBlock({
      tool: { name: 'Edit', input: { file_path: 'a.js' } },
    }))).toEqual([]);
  });

  it('reads a MultiEdit as one hunk per edit', () => {
    // Four rewrites in one file read as four hunks, not one incoherent diff.
    expect(diffSegments(toolBlock({
      tool: {
        name: 'MultiEdit',
        input: {
          edits: [
            { old_string: 'one', new_string: '1' },
            { old_string: 'two', new_string: '2' },
            null,
            'nope',
          ],
        },
      },
    }))).toEqual([
      { oldText: 'one', newText: '1' },
      { oldText: 'two', newText: '2' },
    ]);
  });

  it('is empty for a MultiEdit with no edits list', () => {
    expect(diffSegments(toolBlock({
      tool: { name: 'MultiEdit', input: { file_path: 'a.js' } },
    }))).toEqual([]);
  });

  it('reads a Write as all-add, because the payload has no old side', () => {
    // The spec asks for a Write diffed against the file on disk. `toolUse`
    // does not carry it and the panel has no read that would not race the
    // write it is describing — deliberate gap, noted in the delivery entry.
    expect(diffSegments(toolBlock({
      tool: { name: 'Write', input: { file_path: 'new.js', content: 'export {};' } },
    }))).toEqual([{ oldText: '', newText: 'export {};' }]);
  });

  it('reads a NotebookEdit from its new source', () => {
    expect(diffSegments(toolBlock({
      tool: { name: 'NotebookEdit', input: { notebook_path: 'n.ipynb', new_source: 'x = 1' } },
    }))).toEqual([{ oldText: '', newText: 'x = 1' }]);
  });

  it('is empty for a Write with nothing to write', () => {
    expect(diffSegments(toolBlock({
      tool: { name: 'Write', input: { file_path: 'new.js', content: '' } },
    }))).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Turn layout
// ---------------------------------------------------------------------------

describe('groupBlocksByScope', () => {
  const agentRow = (over = {}) => ({ key: 'task-1', task_id: 'task-1', ...over });

  it('leaves a turn with no subagents flat', () => {
    const blocks = [textBlock(), toolBlock()];
    expect(groupBlocksByScope(blocks, null)).toEqual([
      { type: 'block', block: blocks[0] },
      { type: 'block', block: blocks[1] },
    ]);
  });

  it('is empty for a turn with no blocks', () => {
    expect(groupBlocksByScope(null, null)).toEqual([]);
    expect(groupBlocksByScope([], new Map())).toEqual([]);
  });

  it('puts a row immediately after the Task card that spawned it', () => {
    const task = toolBlock({ block_id: 'toolu_task', tool: { name: 'Task' } });
    const after = textBlock({ block_id: 'req-1:b1', content: 'done' });
    const row = agentRow({ tool_use_id: 'toolu_task' });
    const entries = groupBlocksByScope([task, after], new Map([['task-1', row]]));
    expect(entries.map((e) => e.type)).toEqual(['block', 'agent', 'block']);
    expect(entries[1].row).toBe(row);
  });

  it('nests the subagent’s own blocks under its row', () => {
    // One id does both halves: a nested block's `agent_id` is the Task call's
    // tool_use_id, which is also the row's.
    const task = toolBlock({ block_id: 'toolu_task', tool: { name: 'Task' } });
    const inner = toolBlock({ block_id: 'toolu_inner', agent_id: 'toolu_task' });
    const row = agentRow({ tool_use_id: 'toolu_task' });
    const entries = groupBlocksByScope([task, inner], [row]);
    expect(entries).toHaveLength(2);
    expect(entries[1]).toEqual({ type: 'agent', row, blocks: [inner] });
  });

  it('shows a row whose card never arrived, at the end', () => {
    // A running subagent the user cannot see is worse than one out of place.
    const orphan = agentRow({ tool_use_id: 'toolu_missing', task_id: 'task-9' });
    const entries = groupBlocksByScope([textBlock()], [orphan]);
    expect(entries.map((e) => e.type)).toEqual(['block', 'agent']);
    expect(entries[1].blocks).toEqual([]);
  });

  it('shows a row that never reported a tool_use_id', () => {
    const row = agentRow();
    expect(groupBlocksByScope([], [row])).toEqual([
      { type: 'agent', row, blocks: [] },
    ]);
  });

  it('shows a subagent’s blocks even before anything described the subagent', () => {
    // A subagent's first tool call can land before the CLI's task events.
    const inner = toolBlock({ block_id: 'toolu_inner', agent_id: 'toolu_task' });
    expect(groupBlocksByScope([inner], null)).toEqual([
      { type: 'block', block: inner },
    ]);
  });

  it('emits a row exactly once', () => {
    const task = toolBlock({ block_id: 'toolu_task', tool: { name: 'Task' } });
    const row = agentRow({ tool_use_id: 'toolu_task' });
    const entries = groupBlocksByScope([task], [row, row]);
    expect(entries.filter((e) => e.type === 'agent')).toHaveLength(1);
  });

  it('takes rows from a Map or a plain list', () => {
    const row = agentRow({ tool_use_id: 'toolu_task' });
    const fromMap = groupBlocksByScope([], new Map([['task-1', row]]));
    const fromList = groupBlocksByScope([], [row]);
    expect(fromMap).toEqual(fromList);
  });
});

describe('renderTurnBlocks', () => {
  it('draws nothing for a turn with no blocks', () => {
    const host = draw(renderTurnBlocks(stubPanel(), []));
    expect(host.querySelector('.turn-blocks')).toBeNull();
    const other = draw(renderTurnBlocks(stubPanel(), null));
    expect(other.querySelector('.turn-blocks')).toBeNull();
  });

  it('renders the plan once, at the end, however many times it was written', () => {
    const first = toolBlock({
      block_id: 'toolu_t1',
      superseded: true,
      tool: { name: 'TodoWrite', input: { todos: [{ content: 'old', status: 'pending' }] } },
    });
    const second = toolBlock({
      block_id: 'toolu_t2',
      tool: {
        name: 'TodoWrite',
        input: {
          todos: [
            { content: 'Read the file', status: 'completed' },
            { content: 'Write the test', status: 'in_progress', activeForm: 'Writing the test' },
          ],
        },
      },
    });
    const host = draw(renderTurnBlocks(stubPanel(), [first, textBlock(), second]));
    expect(host.querySelectorAll('.todo-list')).toHaveLength(1);
    // And neither TodoWrite call drew a card of its own.
    expect(host.querySelectorAll('.tool-card')).toHaveLength(0);
    expect(text(host.querySelector('.todo-count'))).toBe('1/2');
    // The plan is last, after the prose.
    expect(host.querySelector('.turn-blocks').lastElementChild.classList
      .contains('todo-list')).toBe(true);
  });

  it('marks the plan live until the turn settles', () => {
    const todo = toolBlock({
      tool: { name: 'TodoWrite', input: { todos: [{ content: 'a', status: 'pending' }] } },
    });
    const live = draw(renderTurnBlocks(stubPanel(), [todo]));
    expect(live.querySelector('.todo-list').classList.contains('live')).toBe(true);
    const done = draw(renderTurnBlocks(stubPanel(), [todo], { settled: true }));
    expect(done.querySelector('.todo-list').classList.contains('final')).toBe(true);
  });

  it('links a file the turn just touched, once the turn is settled', () => {
    // The candidate comes from the tool card, not the repo index — a file the
    // agent just created is clickable before the next reindex.
    const blocks = [
      toolBlock({ tool: { name: 'Write', input: { file_path: 'src/brand-new.js', content: 'x' } } }),
      textBlock({ block_id: 'req-1:b1', content: 'Created src/brand-new.js for you.' }),
    ];
    const settled = draw(renderTurnBlocks(stubPanel(), blocks, { settled: true }));
    expect(settled.querySelector('.file-mention')).toBeTruthy();
  });

  it('does not link mentions while the prose is still arriving', () => {
    // Scanning half-written text produces links that flicker as the path
    // finishes typing.
    const blocks = [
      toolBlock({ tool: { name: 'Write', input: { file_path: 'src/brand-new.js', content: 'x' } } }),
      textBlock({ block_id: 'req-1:b1', content: 'Created src/brand-new.js for you.' }),
    ];
    const live = draw(renderTurnBlocks(stubPanel(), blocks));
    expect(live.querySelector('.file-mention')).toBeNull();
  });

  it('renders a subagent’s blocks inside its row, not at turn level', () => {
    const task = toolBlock({ block_id: 'toolu_task', tool: { name: 'Task', input_summary: 'explore' } });
    const inner = toolBlock({ block_id: 'toolu_inner', agent_id: 'toolu_task', tool: { name: 'Grep' } });
    const row = { key: 'task-1', task_id: 'task-1', tool_use_id: 'toolu_task', description: 'Explore' };
    const host = draw(renderTurnBlocks(stubPanel(), [task, inner], { subagents: [row] }));
    const nested = host.querySelector('.subagent-blocks');
    expect(nested.querySelectorAll('.tool-card')).toHaveLength(1);
    expect(nested.querySelector('.tool-card').dataset.tool).toBe('Grep');
    // The Task card itself is still at turn level, outside the row.
    expect(host.querySelector('.turn-blocks > .tool-card').dataset.tool).toBe('Task');
  });
});

describe('renderBlock', () => {
  it('draws nothing for a superseded block', () => {
    // The record stays in the list so block ids do not renumber; only the last
    // plan renders.
    const host = draw(renderBlock(stubPanel(), textBlock({ superseded: true })));
    expect(host.querySelector('.md-content')).toBeNull();
  });

  it('draws nothing for a TodoWrite card', () => {
    const host = draw(renderBlock(stubPanel(), toolBlock({
      tool: { name: 'TodoWrite', input: { todos: [] } },
    })));
    expect(host.querySelector('.tool-card')).toBeNull();
  });

  it('dispatches on kind', () => {
    const panel = stubPanel();
    expect(draw(renderBlock(panel, toolBlock())).querySelector('.tool-card')).toBeTruthy();
    expect(
      draw(renderBlock(panel, { kind: 'thinking', block_id: 'b1', content: 'hmm' }))
        .querySelector('.thinking-region'),
    ).toBeTruthy();
    expect(draw(renderBlock(panel, textBlock())).querySelector('.md-content')).toBeTruthy();
  });

  it('survives a null block', () => {
    expect(() => draw(renderBlock(stubPanel(), null))).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// Text and thinking
// ---------------------------------------------------------------------------

describe('renderTextBlock', () => {
  it('draws nothing for an empty block', () => {
    // The gap between "block opened" and its first chunk.
    const host = draw(renderTextBlock(stubPanel(), textBlock({ content: '' }), [], false));
    expect(host.querySelector('.md-content')).toBeNull();
  });

  it('renders markdown and tags itself with its block id', () => {
    const host = draw(renderTextBlock(
      stubPanel(),
      textBlock({ content: 'a **bold** claim' }),
      [],
      false,
    ));
    const div = host.querySelector('.md-content.block-text');
    expect(div.dataset.blockId).toBe('req-1:b0');
    expect(div.querySelector('strong').textContent).toBe('bold');
  });

  it('links mentions in a block that reported done, mid-turn', () => {
    // A `done` block will not change again, so waiting for the whole turn
    // would leave a finished paragraph un-linked for no reason.
    const host = draw(renderTextBlock(
      stubPanel(),
      textBlock({ content: 'see src/app.js', done: true }),
      ['src/app.js'],
      false,
    ));
    expect(host.querySelector('.file-mention').dataset.file).toBe('src/app.js');
  });
});

describe('renderThinkingBlock', () => {
  const thinking = (over = {}) => ({
    kind: 'thinking',
    block_id: 'req-1:b0',
    content: 'weighing two options',
    ...over,
  });

  it('draws nothing when the mode omitted reasoning entirely', () => {
    // No chunks arrive, so no block exists — the spec's "not an empty one".
    const host = draw(renderThinkingBlock(stubPanel(), thinking({ content: '' })));
    expect(host.querySelector('.thinking-region')).toBeNull();
  });

  it('starts collapsed, with no token count anywhere on the label', () => {
    // No payload carries per-block thinking tokens — `thinkingChunk` has
    // `{block_id, seq, content, done}` and `usage` is one total for the turn.
    // A number here would have to be invented. The spec is corrected instead.
    const host = draw(renderThinkingBlock(stubPanel(), thinking()));
    expect(host.querySelector('.thinking-body')).toBeNull();
    expect(text(host.querySelector('.thinking-caret'))).toBe('▸');
    expect(text(host.querySelector('.thinking-label'))).toBe('Thinking…');
    expect(host.querySelector('.thinking-region').textContent).not.toMatch(/tok/i);
  });

  it('drops the ellipsis once the block is done', () => {
    const host = draw(renderThinkingBlock(stubPanel(), thinking({ done: true })));
    expect(text(host.querySelector('.thinking-label'))).toBe('Thinking');
  });

  it('shows the reasoning verbatim when opened', () => {
    // Not markdown: thinking is the model's own scratch text and rendering it
    // as prose invents structure that is not there.
    const panel = stubPanel();
    const block = thinking({ content: '**not bold**' });
    const host = draw(renderThinkingBlock(panel, block));
    host.querySelector('.thinking-toggle').click();
    render(renderThinkingBlock(panel, block), host);
    const body = host.querySelector('.thinking-body');
    expect(text(body)).toBe('**not bold**');
    expect(body.querySelector('strong')).toBeNull();
    expect(host.querySelector('.thinking-region').classList.contains('expanded')).toBe(true);
    expect(host.querySelector('.thinking-toggle').getAttribute('aria-expanded')).toBe('true');
  });

  it('says in its tooltip that reasoning is not copied or read aloud', () => {
    const host = draw(renderThinkingBlock(stubPanel(), thinking()));
    expect(host.querySelector('.thinking-toggle').getAttribute('title'))
      .toMatch(/not included in copy/i);
  });
});

// ---------------------------------------------------------------------------
// Tool cards
// ---------------------------------------------------------------------------

describe('renderToolCard', () => {
  it('names the tool, its summary and its status in the header', () => {
    const host = draw(renderToolCard(stubPanel(), toolBlock({
      result: { status: 'ok', preview: 'file-a\nfile-b' },
    })));
    const card = host.querySelector('.tool-card');
    expect(card.classList.contains('tool-status-ok')).toBe(true);
    expect(card.dataset.blockId).toBe('toolu_01');
    expect(card.dataset.tool).toBe('Bash');
    expect(text(card.querySelector('.tool-name'))).toBe('Bash');
    expect(text(card.querySelector('.tool-summary'))).toBe('ls -la');
    expect(card.querySelector('.tool-dot').getAttribute('title')).toBe('Finished');
    expect(text(card.querySelector('.tool-caret'))).toBe('▸');
    // Collapsed, so the body is not in the DOM at all.
    expect(card.querySelector('.tool-body')).toBeNull();
  });

  it('gives an MCP call its server as a separate chip', () => {
    const host = draw(renderToolCard(stubPanel(), toolBlock({
      tool: { name: 'mcp__chrome-devtools__take_snapshot', server: 'chrome-devtools', input: {} },
    })));
    expect(text(host.querySelector('.tool-server-chip'))).toBe('chrome-devtools');
    expect(text(host.querySelector('.tool-name'))).toBe('take_snapshot');
    expect(host.querySelector('.tool-card').dataset.tool)
      .toBe('mcp__chrome-devtools__take_snapshot');
  });

  it('marks a call that went through a permission prompt', () => {
    const host = draw(renderToolCard(stubPanel(), toolBlock({
      gated: true,
      result: { status: 'ok' },
    })));
    const chip = host.querySelector('.tool-gated');
    expect(text(chip)).toBe('gated');
    expect(chip.getAttribute('title')).toMatch(/permission prompt/i);
  });

  it('locks a call that is waiting on the user', () => {
    const host = draw(renderToolCard(stubPanel(), toolBlock({ awaiting: true })));
    const dot = host.querySelector('.tool-dot');
    expect(text(dot)).toBe('🔒');
    expect(dot.getAttribute('title')).toBe('Waiting for your permission');
    expect(host.querySelector('.tool-card').classList.contains('tool-status-awaiting'))
      .toBe(true);
  });

  it('opens a failed call and shows what came back', () => {
    const host = draw(renderToolCard(stubPanel(), toolBlock({
      result: { status: 'error', preview: 'command not found: gti' },
    })));
    expect(host.querySelector('.tool-header').getAttribute('aria-expanded')).toBe('true');
    const result = host.querySelector('.tool-result');
    expect(result.classList.contains('tool-result-error')).toBe(true);
    expect(text(result.querySelector('.tool-result-body')))
      .toBe('command not found: gti');
  });

  it('closes on the first click of an auto-opened failure', () => {
    const panel = stubPanel();
    const block = toolBlock({ result: { status: 'error', preview: 'boom' } });
    const host = draw(renderToolCard(panel, block));
    host.querySelector('.tool-header').click();
    render(renderToolCard(panel, block), host);
    expect(host.querySelector('.tool-body')).toBeNull();
    expect(host.querySelector('.tool-header').getAttribute('aria-expanded')).toBe('false');
  });

  it('replaces the input with the denial, and says who denied it', () => {
    // The call never ran: its input is a proposal, and the reason the user
    // gave is the record — the agent read it too and acted on it.
    const host = draw(renderToolCard(stubPanel(), toolBlock({
      tool: { name: 'Bash', input: { command: 'rm -rf /' }, input_summary: 'rm -rf /' },
      denial: { action: 'deny', reason: 'Not on my machine.', resolvedBy: 'matt' },
    })));
    const body = host.querySelector('.tool-body');
    expect(body.classList.contains('tool-body-denied')).toBe(true);
    expect(text(body.querySelector('.tool-denial-label'))).toBe('Denied by matt');
    expect(text(body.querySelector('.tool-denial-reason'))).toBe('Not on my machine.');
    expect(host.querySelector('.tool-input')).toBeNull();
    expect(host.querySelector('.tool-result')).toBeNull();
  });

  it('says so when a denial carried no reason', () => {
    const host = draw(renderToolCard(stubPanel(), toolBlock({
      denial: { action: 'deny' },
    })));
    expect(text(host.querySelector('.tool-denial-label'))).toBe('Denied');
    expect(text(host.querySelector('.tool-denial-reason'))).toBe('No reason given.');
  });

  it('shows the input as JSON when there is no diff in it', () => {
    const panel = stubPanel();
    const block = toolBlock({ result: { status: 'ok' } });
    panel._blockExpansion.set(block.block_id, true);
    const host = draw(renderToolCard(panel, block));
    expect(host.querySelector('.tool-input').textContent)
      .toContain('"command": "ls -la"');
  });

  it('degrades rather than throwing on input it cannot serialise', () => {
    // A render pass that throws takes the whole panel with it.
    const panel = stubPanel();
    const circular = { file_path: 'a.js' };
    circular.self = circular;
    const block = toolBlock({ tool: { name: 'Read', input: circular }, result: { status: 'ok' } });
    panel._blockExpansion.set(block.block_id, true);
    expect(() => draw(renderToolCard(panel, block))).not.toThrow();
  });

  it('omits the input pane when there is nothing in it', () => {
    const panel = stubPanel();
    const block = toolBlock({ tool: { name: 'TodoRead', input: {} }, result: { status: 'ok', preview: 'x' } });
    panel._blockExpansion.set(block.block_id, true);
    const host = draw(renderToolCard(panel, block));
    expect(host.querySelector('.tool-input')).toBeNull();
    expect(host.querySelector('.tool-result-body')).toBeTruthy();
  });

  it('shows an edit as a diff instead of its raw input', () => {
    const panel = stubPanel();
    const block = toolBlock({
      tool: {
        name: 'MultiEdit',
        input: {
          file_path: 'a.js',
          edits: [
            { old_string: 'one', new_string: '1' },
            { old_string: 'two', new_string: '2' },
          ],
        },
      },
      result: { status: 'ok' },
    });
    panel._blockExpansion.set(block.block_id, true);
    const host = draw(renderToolCard(panel, block));
    expect(host.querySelectorAll('.edit-body')).toHaveLength(2);
    expect(host.querySelector('.tool-input')).toBeNull();
  });

  it('names how much was withheld instead of offering a button', () => {
    // The untruncated text never leaves the server, so "show all" would
    // expand to the same preview it already showed.
    const panel = stubPanel();
    const block = toolBlock({
      result: { status: 'ok', preview: 'first 200 lines…', truncated: true, full_bytes: 2560 },
    });
    panel._blockExpansion.set(block.block_id, true);
    const host = draw(renderToolCard(panel, block));
    const marker = host.querySelector('.tool-result-truncated');
    expect(text(marker)).toBe('Truncated — 2.5 KB in full. The engine sends a preview only.');
    expect(host.querySelector('.tool-body button')).toBeNull();
  });

  it('still marks truncation when the full size is unknown', () => {
    const panel = stubPanel();
    const block = toolBlock({ result: { status: 'ok', preview: 'x', truncated: true } });
    panel._blockExpansion.set(block.block_id, true);
    const host = draw(renderToolCard(panel, block));
    expect(text(host.querySelector('.tool-result-truncated')))
      .toBe('Truncated. The engine sends a preview only.');
  });

  it('says "No output." rather than showing an empty pane', () => {
    const panel = stubPanel();
    const block = toolBlock({ result: { status: 'ok', preview: '' } });
    panel._blockExpansion.set(block.block_id, true);
    const host = draw(renderToolCard(panel, block));
    expect(text(host.querySelector('.tool-result-empty'))).toBe('No output.');
    expect(host.querySelector('.tool-result-body')).toBeNull();
  });

  it('footers the duration and the files it changed', () => {
    const host = draw(renderToolCard(stubPanel(), toolBlock({
      tool: { name: 'Edit', input: { file_path: 'src/a.js' } },
      result: { status: 'ok', duration_ms: 1540, files_modified: ['src/a.js', 'src/b.js'] },
    })));
    const footer = host.querySelector('.tool-footer');
    expect(text(footer.querySelector('.tool-duration'))).toBe('1.5s');
    expect([...footer.querySelectorAll('.tool-file-chip')].map((c) => text(c)))
      .toEqual(['src/a.js', 'src/b.js']);
  });

  it('has no footer when there is nothing to put in it', () => {
    const host = draw(renderToolCard(stubPanel(), toolBlock({ result: { status: 'ok' } })));
    expect(host.querySelector('.tool-footer')).toBeNull();
  });
});

describe('renderFileChip', () => {
  it('asks the shell to open the file, and does not toggle the card on the way', () => {
    // The chip lives inside the card header's click target; without
    // stopPropagation, opening a file would also collapse the card.
    const host = draw(renderFileChip('src/a.js'));
    const outer = vi.fn();
    host.addEventListener('click', outer);
    const navigate = vi.fn();
    window.addEventListener('navigate-file', navigate);
    try {
      host.querySelector('.tool-file-chip')
        .dispatchEvent(new MouseEvent('click', { bubbles: true }));
      expect(navigate).toHaveBeenCalledOnce();
      expect(navigate.mock.calls[0][0].detail).toEqual({ path: 'src/a.js' });
      expect(outer).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener('navigate-file', navigate);
    }
  });

  it('names the file in its tooltip', () => {
    const host = draw(renderFileChip('src/a.js'));
    expect(host.querySelector('.tool-file-chip').getAttribute('title'))
      .toBe('Open src/a.js');
  });
});

// ---------------------------------------------------------------------------
// Todo lists
// ---------------------------------------------------------------------------

describe('renderTodoList', () => {
  it('draws nothing for a turn with no plan', () => {
    expect(draw(renderTodoList([], true)).querySelector('.todo-list')).toBeNull();
    expect(draw(renderTodoList(null, true)).querySelector('.todo-list')).toBeNull();
  });

  it('counts what is finished, not what is in flight', () => {
    const host = draw(renderTodoList([
      { content: 'a', status: 'completed' },
      { content: 'b', status: 'in_progress' },
      { content: 'c', status: 'pending' },
    ], true));
    expect(text(host.querySelector('.todo-count'))).toBe('1/3');
    expect(text(host.querySelector('.todo-title'))).toBe('Plan');
  });

  it('marks each item by status, and falls back to unchecked', () => {
    const host = draw(renderTodoList([
      { content: 'a', status: 'completed' },
      { content: 'b', status: 'in_progress' },
      { content: 'c', status: 'pending' },
      { content: 'd', status: 'deferred' },
      { content: 'e' },
    ], true));
    expect([...host.querySelectorAll('.todo-mark')].map((m) => text(m)))
      .toEqual(['☑', '◐', '☐', '☐', '☐']);
    expect([...host.querySelectorAll('.todo-item')].map((li) => li.className))
      .toEqual([
        'todo-item todo-completed',
        'todo-item todo-in_progress',
        'todo-item todo-pending',
        'todo-item todo-deferred',
        'todo-item todo-pending',
      ]);
  });

  it('reads the running step in the present tense', () => {
    // `activeForm` is what the terminal shows, and it is the correct English
    // for the one step actually happening.
    const host = draw(renderTodoList([
      { content: 'Run the tests', status: 'in_progress', activeForm: 'Running the tests' },
      { content: 'Write the docs', status: 'pending', activeForm: 'Writing the docs' },
    ], true));
    expect([...host.querySelectorAll('.todo-text')].map((t) => text(t)))
      .toEqual(['Running the tests', 'Write the docs']);
  });

  it('leaves an item with no text empty rather than "undefined"', () => {
    const host = draw(renderTodoList([{ status: 'pending' }], true));
    expect(text(host.querySelector('.todo-text'))).toBe('');
  });
});

// ---------------------------------------------------------------------------
// Subagent rows
// ---------------------------------------------------------------------------

describe('renderSubagentRow', () => {
  const row = (over = {}) => ({
    key: 'task-1',
    task_id: 'task-1',
    tool_use_id: 'toolu_task',
    description: 'Explore the auth flow',
    task_type: 'Explore',
    ...over,
  });

  it('draws nothing without a row', () => {
    expect(draw(renderSubagentRow(stubPanel(), null, [], [], false))
      .querySelector('.subagent-row')).toBeNull();
  });

  it('spins a running subagent and offers the one thing a user may do', () => {
    // AC⚡DC did not create this subagent and cannot message it. Stopping it is
    // the only legitimate write.
    const panel = stubPanel();
    const live = row({ status: 'running', last_tool_name: 'Grep', agent_id: 'agent_7' });
    const host = draw(renderSubagentRow(panel, live, [], [], false));
    const el = host.querySelector('.subagent-row');
    expect(el.classList.contains('live')).toBe(true);
    expect(el.dataset.agentId).toBe('agent_7');
    expect(host.querySelector('.subagent-dot').classList.contains('spinning')).toBe(true);
    expect(text(host.querySelector('.subagent-desc'))).toBe('Explore the auth flow');
    expect(text(host.querySelector('.subagent-type'))).toBe('Explore');
    expect(text(host.querySelector('.subagent-status'))).toBe('running');
    expect(text(host.querySelector('.subagent-tool'))).toBe('Grep');

    host.querySelector('.subagent-stop').click();
    expect(panel._stopSubagent).toHaveBeenCalledWith(live);
  });

  it('stops spinning and withdraws Stop once the task is terminal', () => {
    const host = draw(renderSubagentRow(
      stubPanel(),
      row({ terminal: true, status: 'killed', summary: 'Stopped by the user.' }),
      [], [], false,
    ));
    expect(host.querySelector('.subagent-row').classList.contains('terminal')).toBe(true);
    expect(host.querySelector('.subagent-dot').classList.contains('spinning')).toBe(false);
    expect(host.querySelector('.subagent-stop')).toBeNull();
    expect(text(host.querySelector('.subagent-summary'))).toBe('Stopped by the user.');
  });

  it('treats a settled turn as terminal even if the row never latched', () => {
    // A row on a finished message must not spin forever.
    const host = draw(renderSubagentRow(stubPanel(), row(), [], [], true));
    expect(host.querySelector('.subagent-row').classList.contains('terminal')).toBe(true);
    expect(host.querySelector('.subagent-stop')).toBeNull();
  });

  it('cannot offer Stop without a task id to stop', () => {
    const host = draw(renderSubagentRow(stubPanel(), row({ task_id: null }), [], [], false));
    expect(host.querySelector('.subagent-stop')).toBeNull();
  });

  it('shows usage only when the subagent reported some', () => {
    const none = draw(renderSubagentRow(stubPanel(), row({ usage: {} }), [], [], false));
    expect(none.querySelector('.subagent-usage')).toBeNull();
    const some = draw(renderSubagentRow(
      stubPanel(),
      row({ usage: { input_tokens: 12_000, output_tokens: 500 } }),
      [], [], false,
    ));
    expect(text(some.querySelector('.subagent-usage'))).toBe('12.5k tok');
  });

  it('falls back to a name when the CLI described neither the task nor its type', () => {
    const host = draw(renderSubagentRow(
      stubPanel(),
      row({ description: null, task_type: null }),
      [], [], false,
    ));
    expect(text(host.querySelector('.subagent-desc'))).toBe('Subagent');
  });

  it('indents the subagent’s own tool cards beneath it', () => {
    const host = draw(renderSubagentRow(
      stubPanel(),
      row(),
      [toolBlock({ tool: { name: 'Grep', input: {} } })],
      [], false,
    ));
    expect(host.querySelector('.subagent-blocks .tool-card')).toBeTruthy();
  });

  it('has no block container when the subagent has run nothing yet', () => {
    const host = draw(renderSubagentRow(stubPanel(), row(), [], [], false));
    expect(host.querySelector('.subagent-blocks')).toBeNull();
  });

  it('keys off the row key until the CLI reports a transcript id', () => {
    // `agent_id` arrives in the message payload rather than the dataclass, so
    // an early event can have none; the row key is always there.
    const host = draw(renderSubagentRow(stubPanel(), row(), [], [], false));
    expect(host.querySelector('.subagent-row').dataset.agentId).toBe('task-1');
  });
});

// ---------------------------------------------------------------------------
// Turn footer
// ---------------------------------------------------------------------------

describe('renderTurnFooter', () => {
  it('draws nothing without a summary', () => {
    for (const summary of [null, undefined, 'done', 42]) {
      expect(draw(renderTurnFooter(stubPanel(), summary, []))
        .querySelector('.turn-footer')).toBeNull();
    }
  });

  it('draws nothing for a turn with nothing to report', () => {
    // A plain question and answer earns no footer at all.
    const host = draw(renderTurnFooter(stubPanel(), { tool_calls: 0 }, []));
    expect(host.querySelector('.turn-footer')).toBeNull();
  });

  it('leads with the files it changed', () => {
    // "What did it just do to my repo?" is the question the footer answers;
    // everything else on the line is context for it.
    const host = draw(renderTurnFooter(
      stubPanel(),
      { tool_calls: 4, duration_ms: 8200 },
      ['src/a.js', 'src/b.js'],
    ));
    const footer = host.querySelector('.turn-footer');
    expect(footer.firstElementChild.classList.contains('turn-files')).toBe(true);
    expect(text(footer.querySelector('.turn-files-label'))).toBe('2 files modified');
    expect(footer.querySelectorAll('.tool-file-chip')).toHaveLength(2);
  });

  it('counts one file as one file', () => {
    const host = draw(renderTurnFooter(stubPanel(), {}, ['src/a.js']));
    expect(text(host.querySelector('.turn-files-label'))).toBe('1 file modified');
  });

  it('says how many calls were made and how many were asked about', () => {
    const host = draw(renderTurnFooter(
      stubPanel(),
      { tool_calls: 7, permission_prompts: 2 },
      [],
    ));
    expect(text(host.querySelector('.turn-stat'))).toBe('7 tool calls, 2 asked');
  });

  it('says nothing about prompts when nothing was asked', () => {
    const host = draw(renderTurnFooter(stubPanel(), { tool_calls: 1, permission_prompts: 0 }, []));
    expect(text(host.querySelector('.turn-stat'))).toBe('1 tool call');
  });

  it('pairs the wall clock with the engine’s turn count', () => {
    const host = draw(renderTurnFooter(
      stubPanel(),
      { duration_ms: 92_000, num_turns: 3 },
      [],
    ));
    expect(text(host.querySelector('.turn-stat'))).toBe('1m 32s · 3 engine turns');
  });

  it('reports usage per model', () => {
    const host = draw(renderTurnFooter(stubPanel(), {
      turn_model_usage: {
        'claude-opus-5': { inputTokens: 40_000, outputTokens: 2000 },
        'claude-haiku-4-5': { inputTokens: 800 },
      },
    }, []));
    expect([...host.querySelectorAll('.turn-usage')].map((s) => text(s))).toEqual([
      'claude-opus-5 42.0k tok',
      'claude-haiku-4-5 800 tok',
    ]);
  });

  it('reads the live camelCase counters, not just a transcript’s', () => {
    // The failure this fixes: the footer knew only the snake_case spellings a
    // replayed transcript uses, so a live turn summed to zero tokens and
    // rendered no usage lines at all — while the same turn, browsed back
    // later, rendered fine.
    const live = draw(renderTurnFooter(stubPanel(), {
      turn_model_usage: { 'claude-opus-5': { inputTokens: 900, outputTokens: 100 } },
    }, []));
    const replayed = draw(renderTurnFooter(stubPanel(), {
      turn_model_usage: { 'claude-opus-5': { input_tokens: 900, output_tokens: 100 } },
    }, []));
    expect(text(live.querySelector('.turn-usage'))).toBe('claude-opus-5 1.0k tok');
    expect(text(replayed.querySelector('.turn-usage')))
      .toBe(text(live.querySelector('.turn-usage')));
  });

  it('ignores the session’s cumulative usage map', () => {
    // `model_usage` grows all session, so a footer built from it would
    // credit this turn with every model and every token that came before.
    const host = draw(renderTurnFooter(stubPanel(), {
      model_usage: { 'claude-opus-5': { inputTokens: 400_000 } },
      tool_calls: 1,
    }, []));
    expect(host.querySelector('.turn-usage')).toBeNull();
  });

  it('prices the turn, not the session', () => {
    const host = draw(renderTurnFooter(stubPanel(), {
      turn_cost_usd: 0.42,
      turn_cost_basis: 'measured',
      total_cost_usd: 9.99,
    }, []));
    expect(text(host.querySelector('.turn-cost'))).toBe('$0.4200');
  });

  it('distinguishes a turn that cost nothing extra from one it cannot price', () => {
    // The phase 6 exit criterion, at the place the user reads it.
    const free = draw(renderTurnFooter(
      stubPanel(),
      { turn_cost_usd: 0, turn_cost_basis: 'measured' },
      [],
    ));
    expect(text(free.querySelector('.turn-cost'))).toBe('nothing extra');
    expect(free.querySelector('.turn-cost-unknown')).toBeNull();

    const unknown = draw(renderTurnFooter(
      stubPanel(),
      { turn_cost_usd: null, turn_cost_basis: 'unpriced' },
      [],
    ));
    expect(text(unknown.querySelector('.turn-cost'))).toBe('cost unknown');
    expect(unknown.querySelector('.turn-cost-unknown')).toBeTruthy();
    expect(unknown.querySelector('.turn-cost').getAttribute('title'))
      .toMatch(/lands on the next turn/);
  });

  it('says nothing about cost for a turn that never recorded one', () => {
    // A browsed turn: cost is not in the CLI's transcript. "Unknown" on
    // every replayed footer would be noise about a thing never measured.
    const host = draw(renderTurnFooter(stubPanel(), { tool_calls: 1 }, []));
    expect(host.querySelector('.turn-cost')).toBeNull();
    // And the rest of the footer is unaffected.
    expect(host.querySelector('.turn-stat')).toBeTruthy();
  });

  it('warns when the turn never reached the local transcript', () => {
    // Worth a footer of its own: the transcript on disk is now incomplete and
    // nothing else in the UI would say so.
    const host = draw(renderTurnFooter(stubPanel(), { mirror_gap: true }, []));
    const gap = host.querySelector('.turn-mirror-gap');
    expect(text(gap)).toBe('⚠ not mirrored to the local transcript');
    expect(gap.getAttribute('title')).toMatch(/engine health/i);
    expect(host.querySelector('.turn-stats')).toBeNull();
  });

  it('the mirror-gap marker opens the health banner', () => {
    // The link chat.md § Turn Footer promises. It forces the banner open
    // rather than un-dismissing it, so it lands somewhere even when the
    // engine has since recovered.
    const panel = stubPanel({ _healthDismissed: 'old', _healthForced: false });
    const host = draw(renderTurnFooter(panel, { mirror_gap: true }, []));
    host.querySelector('.turn-mirror-gap').click();
    expect(panel._healthForced).toBe(true);
    expect(panel._healthDismissed).toBeNull();
  });
});
