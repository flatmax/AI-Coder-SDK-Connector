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

import { resetRepoRoot, setRepoRoot } from '../repo-path.js';

import {
  blockExpanded,
  compactionSummary,
  diffSegments,
  formatBytes,
  formatDuration,
  formatInvokedAt,
  formatTokens,
  groupBlocksByScope,
  invokedAtMs,
  isDiffTool,
  renderBlock,
  renderFileChip,
  renderLiveUsage,
  renderSubagentRow,
  renderTerminalBadge,
  renderTextBlock,
  renderThinkingBlock,
  renderTodoList,
  renderToolCard,
  renderTurnBlocks,
  renderTurnFooter,
  subagentBlocksExpanded,
  terminalBadge,
  TOOL_SUMMARY_CHARS,
  toggleBlock,
  toggleSubagentBlocks,
  toolInputSummary,
  toolLabel,
} from './block-render.js';
import { STYLES } from './styles.js';

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
  // The repo root is module state, and a chip test that publishes one would
  // otherwise decide how every later test's paths are labelled.
  resetRepoRoot();
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
    // No `input_summary` — the card carries only `input`, and the header's
    // one-liner is rendered from it in the browser (§ C3).
    tool: { name: 'Bash', input: { command: 'ls -la' }, ...tool },
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
  it('starts a call whose body only echoes its header collapsed', () => {
    const panel = stubPanel();
    expect(blockExpanded(panel, textBlock())).toBe(false);
    expect(blockExpanded(panel, { kind: 'thinking', block_id: 'b1' })).toBe(false);
    expect(blockExpanded(panel, toolBlock({ result: { status: 'ok' } }))).toBe(false);
    expect(blockExpanded(panel, toolBlock({
      tool: { name: 'Read', input: { file_path: 'src/a.js' } },
      result: { status: 'ok' },
    }))).toBe(false);
  });

  it('opens a call that failed or was denied', () => {
    // Driven by the status flag, never by sniffing the result text.
    const panel = stubPanel();
    expect(blockExpanded(panel, toolBlock({ result: { status: 'error' } }))).toBe(true);
    expect(blockExpanded(panel, toolBlock({ denial: { action: 'deny' } }))).toBe(true);
  });

  it('opens an edit-shaped call, because the diff is what the card is for', () => {
    // The header names the file and nothing else, so a collapsed Edit row
    // hides the only part of it anyone is scanning for.
    const panel = stubPanel();
    for (const tool of [
      { name: 'Edit', input: { file_path: 'a.js', old_string: 'a', new_string: 'b' } },
      { name: 'Write', input: { file_path: 'a.js', content: 'hello\n' } },
      { name: 'MultiEdit', input: { edits: [{ old_string: 'a', new_string: 'b' }] } },
      { name: 'NotebookEdit', input: { new_source: 'print(1)' } },
    ]) {
      expect(blockExpanded(panel, toolBlock({ tool, result: { status: 'ok' } })))
        .toBe(true);
    }
  });

  it('leaves an edit whose input carries no diff collapsed', () => {
    // An Edit that reached the panel without its strings has nothing to open
    // to, and an empty body under a caret reads as a broken card.
    expect(blockExpanded(stubPanel(), toolBlock({
      tool: { name: 'Edit', input: { file_path: 'a.js' } },
      result: { status: 'ok' },
    }))).toBe(false);
  });

  it('leaves a call waiting on permission collapsed', () => {
    // The dialog is where the user reads it; the card would be a second copy.
    // True of an edit too, which is the one the dialog is showing a diff of.
    expect(blockExpanded(stubPanel(), toolBlock({ awaiting: true }))).toBe(false);
    expect(blockExpanded(stubPanel(), toolBlock({
      tool: { name: 'Edit', input: { file_path: 'a.js', old_string: 'a', new_string: 'b' } },
      awaiting: true,
    }))).toBe(false);
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

describe('invokedAtMs', () => {
  it('parses the ISO string the engine sends', () => {
    expect(invokedAtMs({ invoked_at: '2026-08-27T04:32:07+00:00' }))
      .toBe(Date.UTC(2026, 7, 27, 4, 32, 7));
  });

  it('takes a number as already-epoch milliseconds', () => {
    expect(invokedAtMs({ invoked_at: 1_772_080_327_000 })).toBe(1_772_080_327_000);
  });

  it('is null for a card with no time, rather than now', () => {
    // A card replayed from a transcript entry with no `timestamp`, or from a
    // session recorded before the field existed. Defaulting to now would put
    // a fresh reading on a call made last Tuesday.
    for (const card of [null, undefined, {}, { invoked_at: '' }, { invoked_at: null }]) {
      expect(invokedAtMs(card)).toBeNull();
    }
  });

  it('is null for a timestamp it cannot parse', () => {
    for (const value of ['yesterday', 'NaN', {}, [], NaN, Infinity]) {
      expect(invokedAtMs({ invoked_at: value })).toBeNull();
    }
  });
});

describe('formatInvokedAt', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('is empty for a time there is none of', () => {
    for (const value of [null, undefined, NaN, Infinity]) {
      expect(formatInvokedAt(value)).toBe('');
    }
  });

  it('gives the time of day alone for a call made today', () => {
    // The scanning case: read the chip, glance at your own clock, and you
    // know whether the call has been sitting there for eight minutes.
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 7, 27, 16, 0, 0));
    const invoked = new Date(2026, 7, 27, 14, 32, 7);
    expect(formatInvokedAt(invoked.getTime())).toBe(invoked.toLocaleTimeString());
  });

  it('names the date too once the call is not from today', () => {
    // A bare "14:32:07" on a card replayed from last week's session invites
    // exactly the arithmetic that would be wrong.
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 7, 27, 16, 0, 0));
    const invoked = new Date(2026, 7, 20, 14, 32, 7);
    expect(formatInvokedAt(invoked.getTime())).toBe(invoked.toLocaleString());
  });

  it('counts midnight as another day, not as five hours ago', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 7, 27, 0, 30, 0));
    const invoked = new Date(2026, 7, 26, 23, 55, 0);
    expect(formatInvokedAt(invoked.getTime())).toBe(invoked.toLocaleString());
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

describe('toolInputSummary', () => {
  // The header's one-liner, built here rather than shipped off the engine
  // (§ C3). The whole reason for the move is the first test: the engine's
  // join could not shorten a repo path, so the header spent rows on a prefix
  // identical for every file in the repo.

  it('names a file the way the rest of the app does', () => {
    setRepoRoot('/home/you/repo');
    expect(toolInputSummary({ file_path: '/home/you/repo/src/a.js' }))
      .toBe('file_path=src/a.js');
  });

  it('needs no table of which keys are paths', () => {
    // The blocker `chat.md` recorded, and it was not one: a value that
    // begins with the repo root is a path by its shape, so every string
    // value can be offered to the rule and the rule declines the rest.
    setRepoRoot('/home/you/repo');
    expect(
      toolInputSummary({
        pattern: 'TODO',
        path: '/home/you/repo/webapp/src',
        '-i': true,
      }),
    ).toBe('pattern=TODO path=webapp/src -i=true');
  });

  it('leaves a path outside the repo, and one with no root yet, alone', () => {
    setRepoRoot('/home/you/repo');
    expect(toolInputSummary({ file_path: '/etc/hosts' }))
      .toBe('file_path=/etc/hosts');
    resetRepoRoot();
    expect(toolInputSummary({ file_path: '/home/you/repo/src/a.js' }))
      .toBe('file_path=/home/you/repo/src/a.js');
  });

  it('leaves a path quoted inside a value alone', () => {
    // The rule renames a path; it does not rewrite prose. An `old_string`
    // that happens to quote an absolute path keeps it, because shortening
    // the middle of a value would change text the user is about to compare
    // against their file.
    setRepoRoot('/home/you/repo');
    expect(toolInputSummary({ old_string: 'see /home/you/repo/src/a.js' }))
      .toBe('old_string=see /home/you/repo/src/a.js');
  });

  it('is one line, whatever the input carried', () => {
    // A Bash heredoc arrives with its newlines in it; a header row is one
    // line. Same collapse the engine used to do.
    expect(toolInputSummary({ command: 'echo one\ntwo\tthree' }))
      .toBe('command=echo one two three');
  });

  it('caps the summary, ellipsis included in the count', () => {
    const summary = toolInputSummary({ command: 'x'.repeat(500) });
    expect(summary).toHaveLength(TOOL_SUMMARY_CHARS);
    expect(summary.endsWith('…')).toBe(true);
  });

  it('renders a non-string value as JSON', () => {
    // `[1,2]` where the engine wrote `[1, 2]` — that one space is the only
    // rendering this move changes.
    expect(toolInputSummary({ edits: [1, 2] })).toBe('edits=[1,2]');
  });

  it('loses only the value it cannot serialise', () => {
    // `JSON.stringify` throws on a circular value, and a throw in here is a
    // throw inside a render pass. Caught per value, so the part that could be
    // read still reaches the header.
    const circular = { file_path: 'a.js' };
    circular.self = circular;
    expect(toolInputSummary(circular)).toBe('file_path=a.js self=[object Object]');
  });

  it('is empty for nothing to summarise', () => {
    expect(toolInputSummary(null)).toBe('');
    expect(toolInputSummary({})).toBe('');
    expect(toolInputSummary('file_path=a.js')).toBe('');
    expect(toolInputSummary([1, 2])).toBe('');
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
    const task = toolBlock({ block_id: 'toolu_task', tool: { name: 'Task', input: { prompt: 'explore' } } });
    const inner = toolBlock({ block_id: 'toolu_inner', agent_id: 'toolu_task', tool: { name: 'Grep' } });
    const row = { key: 'task-1', task_id: 'task-1', tool_use_id: 'toolu_task', description: 'Explore' };
    const panel = stubPanel();
    toggleSubagentBlocks(panel, row);
    const host = draw(renderTurnBlocks(panel, [task, inner], { subagents: [row] }));
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
    // `command=ls -la`, not `ls -la`: the fixture used to carry a hand-written
    // `input_summary` that skipped the key, which the engine's own join never
    // did. Rendering from `input` here means the header can only say what the
    // engine would have said.
    expect(text(card.querySelector('.tool-summary'))).toBe('command=ls -la');
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

  describe('the time chip', () => {
    // 14:32:07 local on the day the fake clock is set to.
    const INVOKED = new Date(2026, 7, 27, 14, 32, 7);
    const clock = () => INVOKED.toLocaleTimeString();

    function timedBlock(over = {}) {
      const { tool = {}, ...rest } = over;
      return toolBlock({
        tool: { invoked_at: INVOKED.toISOString(), ...tool },
        ...rest,
      });
    }

    afterEach(() => {
      vi.useRealTimers();
    });

    /** A panel whose run-timer ticker is running, as it is mid-turn. */
    const ticking = () => stubPanel({ _streamTimerInterval: 7 });

    it('says when a finished call was invoked', () => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date(2026, 7, 27, 14, 40, 0));
      const host = draw(renderToolCard(stubPanel(), timedBlock({
        result: { status: 'ok', preview: 'ok', duration_ms: 340 },
      })));
      const chip = host.querySelector('.tool-time');
      expect(text(chip)).toBe(clock());
      expect(chip.getAttribute('title')).toBe(`Invoked at ${clock()} by the engine's clock`);
      // The total belongs to the footer; the header answers "when", not
      // "how long" — a finished call already has its duration below.
      expect(host.querySelector('.tool-elapsed')).toBeNull();
      expect(text(host.querySelector('.tool-duration'))).toBe('340ms');
    });

    it('adds a running elapsed to a pending call — the stall signal', () => {
      // The reason the chip exists: a hung Bash and a Bash that answered in
      // 30ms are otherwise identical on screen, because `duration_ms` only
      // arrives with the result.
      vi.useFakeTimers();
      vi.setSystemTime(new Date(2026, 7, 27, 14, 34, 48));
      const host = draw(renderToolCard(ticking(), timedBlock()));
      const chip = host.querySelector('.tool-time');
      expect(text(host.querySelector('.tool-elapsed'))).toBe('2m 41s');
      // Two spans, stacked in the rail rather than joined by a middle dot:
      // side by side the pair wanted about 110px of a 112px rail, and more
      // than all of it once a restored card puts a date in front of the clock.
      expect(text(chip.querySelector('.tool-clock'))).toBe(clock());
      expect(text(chip)).toBe(`${clock()} 2m 41s`);
      expect(chip.getAttribute('title'))
        .toBe(`Invoked at ${clock()} by the engine's clock — running for 2m 41s`);
    });

    it('times a call that is waiting on the user too', () => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date(2026, 7, 27, 14, 32, 37));
      const host = draw(renderToolCard(ticking(), timedBlock({ awaiting: true })));
      expect(text(host.querySelector('.tool-elapsed'))).toBe('30.0s');
    });

    it('leaves a denied call untimed — that clock measures the reader', () => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date(2026, 7, 27, 14, 40, 0));
      const host = draw(renderToolCard(ticking(), timedBlock({
        denial: { action: 'deny', reason: 'no' },
      })));
      expect(text(host.querySelector('.tool-time'))).toBe(clock());
      expect(host.querySelector('.tool-elapsed')).toBeNull();
    });

    it('withholds the elapsed when no ticker is running to keep it true', () => {
      // Nothing is streaming, so nothing re-renders: a number drawn here
      // would freeze at this value and go on claiming to be live.
      vi.useFakeTimers();
      vi.setSystemTime(new Date(2026, 7, 27, 14, 34, 48));
      const host = draw(renderToolCard(stubPanel(), timedBlock()));
      expect(text(host.querySelector('.tool-time'))).toBe(clock());
      expect(host.querySelector('.tool-elapsed')).toBeNull();
    });

    it('clamps an elapsed the two machines disagree about', () => {
      // The browser subtracts the engine's clock reading from its own. A skew
      // between them must not report a call running for minus four seconds.
      vi.useFakeTimers();
      vi.setSystemTime(new Date(2026, 7, 27, 14, 32, 3));
      const host = draw(renderToolCard(ticking(), timedBlock()));
      expect(text(host.querySelector('.tool-elapsed'))).toBe('0ms');
    });

    it('draws no chip at all for a card that carries no time', () => {
      const host = draw(renderToolCard(ticking(), toolBlock()));
      expect(host.querySelector('.tool-time')).toBeNull();
    });
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
      tool: { name: 'Bash', input: { command: 'rm -rf /' } },
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

  it('names the machine that denied it, and no person', () => {
    // These arrive with `resolved_by` set to the same word as the action,
    // and none of them is anybody's decision — the generic line rendered
    // "cancelled by cancelled".
    for (const [action, label] of [
      ['cancelled', 'Denied — the turn ended before it was answered'],
      ['timeout', 'Denied — no host client was connected to answer'],
      ['shutdown', 'Denied — the session shut down'],
    ]) {
      const host = draw(renderToolCard(stubPanel(), toolBlock({
        denial: { action, reason: 'why', resolvedBy: action },
      })));
      expect(text(host.querySelector('.tool-denial-label'))).toBe(label);
    }
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

  it('shows an edit its diff without a click, and closes on one', () => {
    const panel = stubPanel();
    const block = toolBlock({
      tool: {
        name: 'Edit',
        input: { file_path: 'src/a.js', old_string: 'let a', new_string: 'const a' },
      },
      result: { status: 'ok', preview: 'ok' },
    });
    const host = draw(renderToolCard(panel, block));
    expect(host.querySelector('.tool-header').getAttribute('aria-expanded')).toBe('true');
    expect(host.querySelector('.tool-body')).not.toBeNull();
    expect(host.querySelector('.tool-body').textContent).toContain('const a');

    host.querySelector('.tool-header').click();
    render(renderToolCard(panel, block), host);
    expect(host.querySelector('.tool-body')).toBeNull();
  });

  it('keeps the rules that let a long summary wrap instead of eliding', () => {
    // Read from the source for the reason the file-chip guard below states:
    // jsdom does no layout and does not resolve Lit's adopted stylesheet, so
    // the rules' presence is all that can be checked here.
    //
    // Worth a guard because the header is the *only* place a collapsed card
    // says what the call was about and it carries no tooltip — so an ellipsis
    // put the identifying tail of a path or a flag run out of reach entirely.
    // `anywhere` is the load-bearing word: those strings have no spaces in
    // them, and a summary that may only break at a space cannot break.
    const cssText = STYLES.cssText;
    const at = cssText.indexOf('.tool-summary {');
    expect(at, '.tool-summary rule is gone').toBeGreaterThan(-1);
    const rule = cssText.slice(at, cssText.indexOf('}', at));
    expect(rule).toContain('white-space: pre-wrap');
    expect(rule).toContain('overflow-wrap: anywhere');
    expect(rule).not.toContain('text-overflow');
    expect(rule).not.toContain('nowrap');
    // One bound, not two: `TOOL_SUMMARY_CHARS` in block-render.js already
    // limits the height a card can reach, and a line clamp on top of it would
    // be the same ellipsis three rows lower. Checked across the whole sheet
    // rather than this rule, because the clamp used to live in a Bash-only
    // rule of its own — and the argument for that exception was never about
    // Bash.
    expect(
      cssText,
      'no tool summary should carry a line clamp; if some other rule in this '
        + 'sheet wants one, narrow this check to the tool-card rules',
    ).not.toContain('line-clamp');
    expect(cssText).not.toContain("data-tool='Bash'");
  });

  it('keeps the header two columns, with the chrome in the left one', () => {
    // The structural half of the rule: everything that is not the summary
    // sits in one rail element, so the summary's width is the pane minus the
    // rail and minus nothing else. As three columns — chrome, summary, more
    // chrome — the right-hand group was subtracted from every line of the
    // summary rather than only from the line it was on, because the summary
    // is one box.
    const host = draw(renderToolCard(stubPanel({ _streamTimerInterval: 7 }), toolBlock({
      gated: true,
      tool: { invoked_at: new Date(2026, 7, 27, 14, 32, 7).toISOString() },
    })));
    const header = host.querySelector('.tool-header');
    expect([...header.children].map((el) => el.className))
      .toEqual(['tool-meta', 'tool-summary']);
    // The caret leads the rail: it is the one item that cannot afford to wrap
    // onto a line of its own, and first is the position where it cannot.
    const rail = header.querySelector('.tool-meta');
    expect(text(rail.querySelector('.tool-meta-line > :first-child'))).toBe('▸');
    for (const cls of ['.tool-dot', '.tool-name', '.tool-time', '.tool-gated']) {
      expect(rail.querySelector(cls), `${cls} belongs in the rail`).not.toBeNull();
    }
    // A line each, down the rail: name, then time, then the gated marker.
    // Sharing a line, the marker sat beside the clock on a card timed today
    // and wrapped under it on one restored from another day, so where a
    // reader looked for it depended on how old the card was.
    expect([...rail.children].map((line) => line.lastElementChild.className))
      .toEqual(['tool-name', 'tool-time', 'tool-gated']);
  });

  it('gives the rail no line it would leave empty', () => {
    // Every card written before `invoked_at` existed carries no time, and an
    // ungated one of those has nothing to put below its name.
    const host = draw(renderToolCard(stubPanel(), toolBlock()));
    expect(host.querySelectorAll('.tool-meta-line').length).toBe(1);
    // Gated but untimed: the marker moves up rather than sitting under a gap.
    const gatedHost = draw(renderToolCard(stubPanel(), toolBlock({ gated: true })));
    const lines = gatedHost.querySelectorAll('.tool-meta-line');
    expect(lines.length).toBe(2);
    expect(lines[1].firstElementChild.className).toBe('tool-gated');
  });

  it('fixes the rail width so the summary starts at the same x on every card', () => {
    // A rail as wide as its own widest line is not a column: the summary's
    // left edge would step in and out card by card, and a long MCP tool name
    // or a restored card's date-and-time chip would move it. They wrap inside
    // the rail instead. Read from the source for the reason the guard above
    // states — jsdom does no layout.
    const cssText = STYLES.cssText;
    const at = cssText.indexOf('.tool-header {');
    const rule = cssText.slice(at, cssText.indexOf('}', at));
    expect(rule).toContain('display: grid');
    expect(rule).toMatch(/grid-template-columns: [\d.]+rem 1fr/);
    // The rail aligns with the summary's first line, not with the middle of a
    // block several lines tall.
    expect(rule).toContain('align-items: start');
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

  it('labels an absolute path with its repo-relative name', () => {
    // Every path on a tool card is absolute — Claude Code's file tools
    // require it — and the identifying end of a long one is the end that
    // falls off the chip. next.md § C4.
    setRepoRoot('/home/dev/my-repo');
    const host = draw(
      renderFileChip('/home/dev/my-repo/src/chat-panel/block-render.js'),
    );
    expect(text(host.querySelector('.tool-file-chip')))
      .toBe('src/chat-panel/block-render.js');
  });

  it('keeps the absolute path on the tooltip and in the click', () => {
    // The label is a display concern. `onNavigateFile` is the one
    // normaliser, so shortening the label must not shorten what is
    // dispatched, and the full path stays reachable by hovering.
    setRepoRoot('/home/dev/my-repo');
    const abs = '/home/dev/my-repo/src/a.js';
    const host = draw(renderFileChip(abs));
    const chip = host.querySelector('.tool-file-chip');
    expect(chip.getAttribute('title')).toBe(`Open ${abs}`);
    const navigate = vi.fn();
    window.addEventListener('navigate-file', navigate);
    try {
      chip.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      expect(navigate.mock.calls[0][0].detail).toEqual({ path: abs });
    } finally {
      window.removeEventListener('navigate-file', navigate);
    }
  });

  it('leaves a path outside the repo at its absolute name', () => {
    // It has no other name here, so the absolute path is the label as well
    // as the tooltip — the same non-answer `toRepoPath` gives navigation.
    setRepoRoot('/home/dev/my-repo');
    const host = draw(renderFileChip('/etc/hosts'));
    expect(text(host.querySelector('.tool-file-chip'))).toBe('/etc/hosts');
  });

  it('shows the absolute path before a root is published', () => {
    // No state snapshot yet. Measuring against '' would name the wrong file.
    const host = draw(renderFileChip('/home/dev/my-repo/src/a.js'));
    expect(text(host.querySelector('.tool-file-chip')))
      .toBe('/home/dev/my-repo/src/a.js');
  });

  it('keeps the rules that bound a long path to one row', () => {
    // A regression guard, not a layout test — jsdom does no layout and does
    // not resolve Lit's adopted stylesheet, so the rules are read from the
    // source, same as the slash palette's hint-width guard. The relative
    // label fits the common case; a deeply nested path still does not, and
    // this chip had no width budget at all before § C4.
    const cssText = STYLES.cssText;
    const at = cssText.indexOf('.tool-file-chip {');
    expect(at, '.tool-file-chip rule is gone').toBeGreaterThan(-1);
    const rule = cssText.slice(at, cssText.indexOf('}', at));
    expect(rule).toContain('max-width: 24rem');
    expect(rule).toContain('overflow: hidden');
    expect(rule).toContain('text-overflow: ellipsis');
    expect(rule).toContain('white-space: nowrap');
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
    // Both types as the CLI really reports them: the transport kind and the
    // agent's own kind. Only the second is ever shown to a reader.
    task_type: 'local_agent',
    subagent_type: 'Explore',
    ...over,
  });

  it('draws nothing without a row', () => {
    expect(draw(renderSubagentRow(stubPanel(), null, [], [], false))
      .querySelector('.subagent-row')).toBeNull();
  });

  it('spins a running subagent and offers the one thing a user may do', () => {
    // AIC⚡DC did not create this subagent and cannot message it. Stopping it is
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
    // Never to `task_type`: "local_agent" is the transport, and a row headed
    // with it names nothing a reader was looking for.
    const host = draw(renderSubagentRow(
      stubPanel(),
      row({ description: null, subagent_type: null }),
      [], [], false,
    ));
    expect(text(host.querySelector('.subagent-desc'))).toBe('Subagent');
  });

  it('collapses the subagent’s own tool cards, counting them instead', () => {
    // The transcript has a tab; drawing it inline as well is what made a
    // delegated turn read as though it had happened twice.
    const panel = stubPanel();
    const live = row({ status: 'running' });
    const host = draw(renderSubagentRow(
      panel,
      live,
      [toolBlock({ tool: { name: 'Grep', input: {} } }),
        toolBlock({ tool: { name: 'Read', input: {} } })],
      [], false,
    ));
    expect(host.querySelector('.subagent-blocks')).toBeNull();
    const toggle = host.querySelector('.subagent-blocks-toggle');
    expect(text(toggle)).toBe('▸ 2 tool calls');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');

    toggle.click();
    expect(subagentBlocksExpanded(panel, live)).toBe(true);
    expect(panel.requestUpdate).toHaveBeenCalled();
  });

  it('counts the calls it would draw, not every nested block', () => {
    // A subagent's blocks are its prose as well as its calls, and a
    // `TodoWrite` draws nothing. Observed live: one `Read` between two text
    // blocks was announced as "3 tool calls".
    const host = draw(renderSubagentRow(
      stubPanel(),
      row(),
      [
        textBlock({ block_id: 'a:b0', content: 'I’ll read the README.' }),
        toolBlock({ tool: { name: 'Read', input: {} } }),
        toolBlock({ block_id: 'toolu_todo', tool: { name: 'TodoWrite', input: {} } }),
        textBlock({ block_id: 'a:b1', content: 'The magic word is ORCHID.' }),
      ],
      [], false,
    ));
    expect(text(host.querySelector('.subagent-blocks-toggle'))).toBe('▸ 1 tool call');
  });

  it('names the disclosure for a subagent that only answered', () => {
    const host = draw(renderSubagentRow(
      stubPanel(),
      row(),
      [textBlock({ block_id: 'a:b0', content: 'ORCHID.' })],
      [], false,
    ));
    expect(text(host.querySelector('.subagent-blocks-toggle'))).toBe('▸ transcript');
  });

  it('indents the subagent’s own tool cards beneath it once expanded', () => {
    const panel = stubPanel();
    const one = row();
    toggleSubagentBlocks(panel, one);
    const host = draw(renderSubagentRow(
      panel,
      one,
      [toolBlock({ tool: { name: 'Grep', input: {} } })],
      [], false,
    ));
    expect(host.querySelector('.subagent-blocks .tool-card')).toBeTruthy();
    expect(text(host.querySelector('.subagent-blocks-toggle'))).toBe('▾ 1 tool call');
  });

  it('does not offer a disclosure for a subagent that has run nothing', () => {
    const host = draw(renderSubagentRow(stubPanel(), row(), [], [], false));
    expect(host.querySelector('.subagent-blocks-toggle')).toBeNull();
  });

  it('remembers one subagent’s disclosure without opening the next', () => {
    // The same `_blockExpansion` Map serves rows and blocks alike, keyed by
    // task id, so expanding one row must not expand its siblings.
    const panel = stubPanel();
    toggleSubagentBlocks(panel, row({ key: 'task-1' }));
    expect(subagentBlocksExpanded(panel, row({ key: 'task-1' }))).toBe(true);
    expect(subagentBlocksExpanded(panel, row({ key: 'task-2' }))).toBe(false);
  });

  it('renders the summary as the markdown the subagent wrote', () => {
    // Verified against a live CLI notification: the summary is the subagent's
    // own closing answer, and it arrives as markdown.
    const host = draw(renderSubagentRow(
      stubPanel(),
      row({ terminal: true, summary: 'The magic word is **ORCHID**.' }),
      [], [], false,
    ));
    const summary = host.querySelector('.subagent-summary');
    expect(text(summary)).toBe('The magic word is ORCHID.');
    expect(summary.querySelector('strong').textContent).toBe('ORCHID');
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
      'claude-opus-5 42.0k tok · 40.0k in · 2.0k out',
      'claude-haiku-4-5 800 tok · 800 in',
    ]);
  });

  it('splits cache traffic out of the prompt rather than into "in"', () => {
    // The whole reason the chip is three-way: `inputTokens` is the *uncached
    // remainder*, so folding cache reads into it would report a cheap cached
    // turn at the full-price rate — off by 10x on the part that dominates.
    const host = draw(renderTurnFooter(stubPanel(), {
      turn_model_usage: {
        'claude-opus-5': {
          inputTokens: 300,
          outputTokens: 2000,
          cacheCreationInputTokens: 1200,
          cacheReadInputTokens: 40_000,
        },
      },
    }, []));
    const chip = host.querySelector('.turn-usage');
    expect(text(chip)).toBe('claude-opus-5 43.5k tok · 300 in · 41.2k cache · 2.0k out');
    // And the tooltip separates the two cache figures, which are priced
    // differently — a read is ~0.1x input, a write ~1.25x.
    expect(chip.getAttribute('title')).toContain('40,000 read from the prompt cache');
    expect(chip.getAttribute('title')).toContain('1,200 written to the cache');
    expect(chip.getAttribute('title')).toContain('41,500 tokens in all');
  });

  it('drops a counter it has no measurement for', () => {
    // "0 cache" reads as a measured zero. The engine simply did not report
    // one, and the chip must not claim otherwise.
    const host = draw(renderTurnFooter(stubPanel(), {
      turn_model_usage: { 'claude-opus-5': { cacheReadInputTokens: 5000 } },
    }, []));
    expect(text(host.querySelector('.turn-usage')))
      .toBe('claude-opus-5 5.0k tok · 5.0k cache');
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
    expect(text(live.querySelector('.turn-usage')))
      .toBe('claude-opus-5 1.0k tok · 900 in · 100 out');
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

describe('renderLiveUsage', () => {
  it('renders nothing until the engine has counted something', () => {
    // Every one of these is a moment the streaming card is on screen: before
    // the first assistant message lands, and on a replayed turn whose stream
    // snapshot carried no counters. An empty "so far" row would be furniture.
    for (const usage of [null, undefined, {}, { turn_model_usage: null }]) {
      expect(draw(renderLiveUsage(usage)).textContent.trim()).toBe('');
    }
  });

  it('labels the running figure so it is not read as the total', () => {
    const host = draw(renderLiveUsage({
      turn_model_usage: { 'claude-opus-5': { input_tokens: 900, output_tokens: 100 } },
    }));
    expect(text(host.querySelector('.turn-live-label'))).toBe('so far');
    expect(text(host.querySelector('.turn-usage')))
      .toBe('claude-opus-5 1.0k tok · 900 in · 100 out');
  });

  it('is the same chip the settled footer draws', () => {
    // One renderer for both, so the number cannot appear to jump when the
    // result message replaces the running figure with its own.
    const payload = {
      turn_model_usage: {
        'claude-opus-5': { input_tokens: 300, cache_read_input_tokens: 40_000 },
      },
    };
    expect(text(draw(renderLiveUsage(payload)).querySelector('.turn-usage')))
      .toBe(text(draw(renderTurnFooter(stubPanel(), payload, [])).querySelector('.turn-usage')));
  });

  it('gives a subagent its own line', () => {
    // The engine counts subagent messages under their own model, and a turn
    // that spawned four Haiku agents spent that money too.
    const host = draw(renderLiveUsage({
      turn_model_usage: {
        'claude-opus-5': { input_tokens: 900 },
        'claude-haiku-4-5': { input_tokens: 4000 },
      },
    }));
    expect([...host.querySelectorAll('.turn-usage')].map((s) => text(s))).toEqual([
      'claude-haiku-4-5 4.0k tok · 4.0k in',
      'claude-opus-5 900 tok · 900 in',
    ]);
  });

  it('shows no cost, which cannot be known mid-turn', () => {
    // Pricing this turn needs the result message's differenced session total;
    // anything shown before it would be a guess, and chat.md § Turn Footer
    // forbids numbers AIC⚡DC computed itself.
    const host = draw(renderLiveUsage({
      turn_model_usage: { 'claude-opus-5': { input_tokens: 900, costUSD: 0.02 } },
    }));
    expect(host.querySelector('.turn-cost')).toBeNull();
    expect(host.textContent).not.toContain('$');
  });
});
