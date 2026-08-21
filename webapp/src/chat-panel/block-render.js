// Rendering for the Claude Code block stream.
//
// `blocks.js` decides what a turn *is*; this module decides what it looks
// like. Both halves are needed because the block list is not a flat run of
// prose any more: a turn interleaves text, thinking, tool calls with their
// results, a live plan, and whole subagents, and each of those has its own
// collapse behaviour and its own idea of what "finished" means.
//
// Everything here is either a Lit template or a pure helper. The pure ones are
// exported separately so the interesting decisions — which badge a terminal
// reason earns, whether a tool card starts expanded, how a turn's blocks group
// under their subagent — can be tested without a DOM.
//
// Expansion state lives on the panel as `_blockExpansion`, a Map of block id →
// boolean. Block ids are globally unique (`{request_id}:b{n}` for text and
// thinking, the SDK's `tool_use_id` for tools), so one Map serves every tab
// and every settled message without keying by tab. That is what gives
// specs5/5-webapp/chat.md's "expanded state is per-block and remembered for
// the session" for free — the Map outlives the turn, and a user who expanded
// one thinking region does not get the next one expanded with it.

import { html, nothing } from 'lit';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';

import { renderEditBody } from '../edit-block-render.js';
import { findFileMentions } from '../file-mentions.js';
import { renderMarkdown } from '../markdown.js';
import { costLabel, modelUsageLines, taskUsage } from '../turn-cost.js';

import { collectToolPaths, isTodoWrite, latestTodos, toolStatus } from './blocks.js';
import { revealHealth } from './health-banner.js';

// ---------------------------------------------------------------
// Terminal-reason badge
// ---------------------------------------------------------------

/**
 * Terminal reasons that mean "the turn ended because it was finished".
 *
 * Deliberately a set of one. The engine reports an open vocabulary and new
 * values will appear as the CLI grows; treating an unrecognised reason as a
 * success would be the one failure mode worth avoiding, since a badge that
 * claims a clean finish is worse than no badge at all.
 */
const NATURAL_REASONS = new Set(['completed']);

/**
 * Reasons the *user* caused. Information, not warning — but always visible,
 * because an interrupted turn may have left a half-finished edit on disk
 * (specs5/5-webapp/chat.md § Terminal-Reason Badge Placement).
 */
const INTERRUPTION_REASONS = new Set(['aborted_streaming', 'aborted_tools']);

const REASON_LABELS = {
  completed: 'completed',
  aborted_streaming: 'interrupted',
  aborted_tools: 'interrupted mid-tool',
  max_turns: 'turn limit reached',
  refusal: 'refused',
  engine_error: 'engine error',
  session_lost: 'session lost',
};

/**
 * How a `terminal_reason` should be badged: its label, its severity, and
 * where on the card it belongs.
 *
 * Null for a null reason. Older CLIs and local slash-command results report
 * nothing, and the spec is explicit that no badge is drawn rather than one
 * claiming success.
 *
 * @param {string|null|undefined} reason
 * @returns {{label: string, severity: string, placement: string}|null}
 */
export function terminalBadge(reason) {
  if (typeof reason !== 'string' || !reason) return null;
  if (NATURAL_REASONS.has(reason)) {
    return { label: REASON_LABELS[reason], severity: 'natural', placement: 'footer' };
  }
  if (INTERRUPTION_REASONS.has(reason)) {
    return { label: REASON_LABELS[reason], severity: 'neutral', placement: 'header' };
  }
  // Everything else — limits, refusals, engine failures, and values this
  // build has never seen — reads at the top of the card next to the role
  // label, where users notice it before reading the body.
  return {
    label: REASON_LABELS[reason] || reason.replace(/_/g, ' '),
    severity: 'error',
    placement: 'header',
  };
}

/**
 * The badge itself. `reason` is kept in the tooltip verbatim so an unmapped
 * value is still diagnosable from the UI.
 */
export function renderTerminalBadge(reason) {
  const badge = terminalBadge(reason);
  if (!badge) return nothing;
  return html`
    <span
      class="finish-reason-badge severity-${badge.severity}"
      title="terminal_reason: ${reason}"
    >${badge.severity === 'natural' ? '✓ ' : ''}${badge.label}</span>
  `;
}

// ---------------------------------------------------------------
// Expansion state
// ---------------------------------------------------------------

/**
 * Whether a block's body is showing.
 *
 * An explicit click always wins. Absent one, three kinds of card open
 * themselves:
 *
 *   - A call that failed or was denied, driven by the status flag and never
 *     by string-sniffing the result text (specs5/5-webapp/chat.md § Card
 *     Anatomy).
 *   - An edit-shaped call, because the diff *is* what the card is about: the
 *     header names the file and nothing else, so a collapsed `Edit` row hides
 *     the only part a reader is scanning for. An earlier draft kept these
 *     collapsed to stop a nine-edit turn opening nine diffs; in practice the
 *     hunks are small, the pane scrolls, and clicking nine carets to read a
 *     turn was the worse trade.
 *   - Nothing else. A `Read` or a `Bash` card's body is a JSON echo of a
 *     header that already summarises it.
 *
 * A call still waiting on permission stays shut whatever its shape: the
 * dialog is open over the top with the same diff in it, and the card would be
 * a second copy of a decision the user is in the middle of making.
 */
export function blockExpanded(panel, block) {
  const explicit = panel?._blockExpansion?.get(block?.block_id);
  if (typeof explicit === 'boolean') return explicit;
  if (block?.kind !== 'tool') return false;
  const status = toolStatus(block);
  if (status === 'error' || status === 'denied') return true;
  if (status === 'awaiting') return false;
  return diffSegments(block).length > 0;
}

/**
 * Flip a block's expansion and repaint.
 *
 * Reads the current *effective* state rather than the stored one, so the first
 * click on an auto-expanded error card collapses it instead of appearing to do
 * nothing.
 */
export function toggleBlock(panel, block) {
  if (!panel || !block?.block_id) return;
  if (!panel._blockExpansion) panel._blockExpansion = new Map();
  panel._blockExpansion.set(block.block_id, !blockExpanded(panel, block));
  panel.requestUpdate();
}

// ---------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------

export function formatDuration(ms) {
  if (!Number.isFinite(ms) || ms < 0) return '';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const mins = Math.floor(seconds / 60);
  return `${mins}m ${Math.round(seconds - mins * 60)}s`;
}

export function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return '';
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatTokens(count) {
  if (!Number.isFinite(count) || count < 0) return '';
  if (count < 1000) return String(Math.round(count));
  if (count < 1_000_000) return `${(count / 1000).toFixed(1)}k`;
  return `${(count / 1_000_000).toFixed(2)}M`;
}

// Token counting, the per-model lines and the cost wording all moved to
// ../turn-cost.js — see the import at the top of this file. They were local
// helpers that knew only the snake_case spellings a replayed transcript uses,
// so a *live* turn, whose per-model counters arrive in the engine's camelCase,
// summed to zero and rendered no usage lines at all. The usage HUD read the
// same numbers and got them wrong differently. One owner now.

// ---------------------------------------------------------------
// Per-model usage chips
// ---------------------------------------------------------------

/**
 * One per-model usage line as a chip: the total, then how it split.
 *
 * The total leads, because it answers "how big was this turn" and it is the
 * figure this chip has always shown. The split follows, because the parts are
 * priced between 0.1× and 5× each other and one number cannot say which kind
 * of tokens a turn spent — a 50k turn that was almost all cache reads and one
 * that was almost all output are the same chip otherwise, and about twenty
 * times apart in cost.
 *
 * A zero part is dropped rather than printed. A replayed transcript that
 * recorded no cache counters would otherwise read "0 cache", which claims a
 * measurement it never made — the same rule the cost chip follows.
 *
 * `in` is deliberately not "input": it counts only the part of the prompt
 * charged at full price, and the cached part is prompt too. There is no room
 * on a chip to say that, so the tooltip says it.
 */
export function renderUsageChip(line) {
  const parts = [];
  if (line.input > 0) parts.push(`${formatTokens(line.input)} in`);
  if (line.cache > 0) parts.push(`${formatTokens(line.cache)} cache`);
  if (line.output > 0) parts.push(`${formatTokens(line.output)} out`);
  const split = parts.length > 0 ? ` · ${parts.join(' · ')}` : '';
  return html`
    <span class="turn-stat turn-usage" title=${usageTitle(line)}
      >${line.model} ${formatTokens(line.tokens)} tok${split}</span
    >
  `;
}

/**
 * The sentence behind a usage chip: every counter, unrounded, and what the
 * chip's `in` does not include.
 *
 * Cache reads and cache writes are one chip part but two tooltip clauses:
 * they are the same tokens from the reader's point of view and very different
 * ones from the bill's, so the detail belongs here rather than in the chip.
 */
export function usageTitle(line) {
  const n = (value) => Math.round(value).toLocaleString();
  const parts = [`${n(line.input)} input at full price`];
  if (line.cacheRead > 0) {
    parts.push(`${n(line.cacheRead)} read from the prompt cache`);
  }
  if (line.cacheCreation > 0) {
    parts.push(`${n(line.cacheCreation)} written to the cache`);
  }
  parts.push(`${n(line.output)} output`);
  return `${line.model} — ${n(line.tokens)} tokens this turn: `
    + `${parts.join(', ')}. The prompt was ${n(line.prompt)} tokens in all; `
    + 'the "in" figure counts only the part of it that was not cached.';
}

/**
 * The running token counter under a streaming turn.
 *
 * The same chips the settled footer draws, from the same `turn_model_usage`
 * shape: the engine pushes a per-model running total as each assistant
 * message reports what it used (`turnUsage`), so this is what the engine
 * measured and not a count of our own — AIC⚡DC never counts tokens itself
 * (specs5/5-webapp/chat.md § Live Token Counter).
 *
 * Labelled "so far" because that is what it is. Usage arrives per completed
 * assistant message rather than per token, so the numbers step up a few times
 * a turn — several times more on an agentic turn, once on a one-shot answer —
 * and the result message replaces them with its own when it lands.
 *
 * No cost. Pricing a turn means differencing the session's running total
 * against itself, which only the result message carries; anything else here
 * would be a guess wearing a dollar sign.
 */
export function renderLiveUsage(usage) {
  const lines = modelUsageLines(usage);
  if (lines.length === 0) return nothing;
  return html`
    <div class="turn-stats turn-live-usage">
      <span class="turn-stat turn-live-label">so far</span>
      ${lines.map((line) => renderUsageChip(line))}
    </div>
  `;
}

// ---------------------------------------------------------------
// Compaction boundary
// ---------------------------------------------------------------

/**
 * What a `compact_boundary` event has to say, as display-ready parts.
 *
 * Shared by the two places that need it: the event handler, which stores a
 * plain-text `content` so search, copy and a later transcript reload all have
 * something to work with, and the divider renderer, which lays the same facts
 * out as a rule with a label. One builder so the two never disagree.
 *
 * Every field is optional on the wire. The subtype falls through to an untyped
 * SystemMessage in the SDK (see claude_code/messages.py::_system_message), so a
 * CLI that renames or drops `pre_tokens` produces a divider that still says
 * compaction happened rather than one that says `undefined → undefined`.
 *
 * `trigger` is mapped for the two values the CLI is known to send and passed
 * through verbatim otherwise — an unrecognised trigger is worth seeing, not
 * worth hiding.
 */
export function compactionSummary(compaction) {
  const c = compaction && typeof compaction === 'object' ? compaction : {};
  const pre = Number.isFinite(c.pre_tokens) && c.pre_tokens >= 0 ? c.pre_tokens : null;
  const post = Number.isFinite(c.post_tokens) && c.post_tokens >= 0 ? c.post_tokens : null;
  const raw = typeof c.trigger === 'string' && c.trigger ? c.trigger : null;
  const trigger =
    raw === 'auto' ? 'automatic' : raw === 'manual' ? 'manual' : raw;
  let counts = '';
  if (pre !== null && post !== null) {
    counts = `${formatTokens(pre)} → ${formatTokens(post)} tokens`;
  } else if (pre !== null) {
    counts = `from ${formatTokens(pre)} tokens`;
  } else if (post !== null) {
    counts = `to ${formatTokens(post)} tokens`;
  }
  const parts = ['Context compacted'];
  if (trigger) parts.push(`(${trigger})`);
  if (counts) parts.push(`— ${counts}`);
  return { pre, post, trigger, counts, text: parts.join(' ') };
}

/**
 * A tool's display name: the bare tool for an MCP call, whose server is shown
 * as its own chip, and the name verbatim otherwise.
 */
export function toolLabel(card) {
  const name = typeof card?.name === 'string' ? card.name : '';
  if (!name.startsWith('mcp__')) return name;
  const parts = name.split('__');
  return parts.length >= 3 ? parts.slice(2).join('__') : name;
}

// ---------------------------------------------------------------
// Diffs for edit-shaped tools
// ---------------------------------------------------------------

const DIFF_TOOLS = new Set(['Edit', 'MultiEdit', 'Write', 'NotebookEdit']);

/**
 * True when a tool's input is a diff in disguise.
 *
 * Matched on the bare name so an MCP server re-exporting `Edit` gets the same
 * treatment as the built-in.
 */
export function isDiffTool(name) {
  if (typeof name !== 'string' || !name) return false;
  const bare = name.startsWith('mcp__') ? toolLabel({ name }) : name;
  return DIFF_TOOLS.has(bare);
}

/**
 * The diff (or diffs) a tool call's input describes, or an empty array when it
 * describes none.
 *
 * `Edit` is one old→new pair. `MultiEdit` is a list of them, each rendered
 * separately so a call that rewrites four places in a file reads as four
 * hunks rather than one incoherent diff. `Write` and `NotebookEdit` carry only
 * the new content: rendering it against an empty old side produces an all-add
 * diff, which is the honest rendering of what the payload contains.
 *
 * The spec also wants a `Write` diffed against the file on disk when one
 * exists. The `toolUse` payload does not carry the old content and the panel
 * has no read path that could fetch it without racing the write it is
 * describing, so a `Write` renders as new content here. Deliberate gap, noted
 * in the phase-2 delivery entry.
 */
export function diffSegments(block) {
  const card = block?.tool;
  if (!card || !isDiffTool(card.name)) return [];
  const input = card.input;
  if (!input || typeof input !== 'object') return [];
  const bare = toolLabel(card);
  if (bare === 'MultiEdit') {
    const edits = Array.isArray(input.edits) ? input.edits : [];
    return edits
      .filter((edit) => edit && typeof edit === 'object')
      .map((edit) => ({
        oldText: typeof edit.old_string === 'string' ? edit.old_string : '',
        newText: typeof edit.new_string === 'string' ? edit.new_string : '',
      }));
  }
  if (bare === 'Edit') {
    if (typeof input.old_string !== 'string' && typeof input.new_string !== 'string') {
      return [];
    }
    return [{
      oldText: typeof input.old_string === 'string' ? input.old_string : '',
      newText: typeof input.new_string === 'string' ? input.new_string : '',
    }];
  }
  const content = bare === 'NotebookEdit' ? input.new_source : input.content;
  if (typeof content !== 'string' || !content) return [];
  return [{ oldText: '', newText: content }];
}

// ---------------------------------------------------------------
// Turn layout
// ---------------------------------------------------------------

/**
 * Arrange a turn's blocks into render order, with each subagent's blocks
 * nested under its row.
 *
 * One id does both halves of the job. A block produced inside a subagent
 * carries `agent_id = parent_tool_use_id` — the id of the `Task` call that
 * spawned it, not the subagent's transcript key — and a `subagentEvent`
 * carries that same id as `tool_use_id`. So `row.tool_use_id` matches both the
 * `Task` card to sit under and the blocks to nest, with no correlation table.
 *
 * Rows with no matching card — a replay that lost the card, a task the CLI
 * reported before the assistant message describing it — land at the end rather
 * than being dropped: a running subagent the user cannot see is worse than one
 * in the wrong place.
 *
 * Returns a flat list of entries so the caller does no grouping of its own:
 *   `{type: 'block', block}` — render at turn level
 *   `{type: 'agent', row, blocks}` — render the row, then its blocks indented
 *
 * @param {Array} blocks
 * @param {Map|Iterable} subagents
 */
export function groupBlocksByScope(blocks, subagents) {
  const list = Array.isArray(blocks) ? blocks : [];
  const rows = subagents ? [...(subagents.values?.() ?? subagents)] : [];

  // Blocks attributed to a subagent, bucketed by the Task call they came from.
  const nested = new Map();
  const main = [];
  for (const block of list) {
    if (block?.agent_id) {
      const bucket = nested.get(block.agent_id);
      if (bucket) bucket.push(block);
      else nested.set(block.agent_id, [block]);
    } else {
      main.push(block);
    }
  }

  const byToolUse = new Map();
  for (const row of rows) {
    if (!row?.tool_use_id) continue;
    const bucket = byToolUse.get(row.tool_use_id);
    if (bucket) bucket.push(row);
    else byToolUse.set(row.tool_use_id, [row]);
  }

  const entries = [];
  const emitted = new Set();
  const claimed = new Set();
  const emitRow = (row) => {
    emitted.add(row);
    if (row.tool_use_id) claimed.add(row.tool_use_id);
    entries.push({ type: 'agent', row, blocks: nested.get(row.tool_use_id) || [] });
  };

  for (const block of main) {
    entries.push({ type: 'block', block });
    if (block?.kind !== 'tool') continue;
    for (const row of byToolUse.get(block.block_id) || []) {
      if (!emitted.has(row)) emitRow(row);
    }
  }
  for (const row of rows) {
    if (!emitted.has(row)) emitRow(row);
  }

  // Blocks from a Task call that has no row at all. The card carries its scope
  // before the CLI's task events necessarily arrive, so this is a real
  // ordering rather than a defensive branch — a subagent's first tool call can
  // land before anything has described the subagent.
  for (const [parentId, bucket] of nested) {
    if (claimed.has(parentId)) continue;
    for (const block of bucket) entries.push({ type: 'block', block });
  }

  return entries;
}

/**
 * Render a turn's blocks.
 *
 * `settled` distinguishes a finished turn from a running one, and it changes
 * two things: file mentions are only detected on final content (scanning
 * half-written prose produces links that flicker as the path completes), and
 * the live plan collapses to its final state.
 *
 * @param {Object} panel
 * @param {Array} blocks
 * @param {Object} [options]
 * @param {Map} [options.subagents]
 * @param {boolean} [options.settled]
 */
export function renderTurnBlocks(panel, blocks, options = {}) {
  const list = Array.isArray(blocks) ? blocks : [];
  if (list.length === 0) return nothing;
  const settled = !!options.settled;

  // Mention candidates are the repo index plus every path this turn touched,
  // so a file the agent just created is clickable before the next reindex
  // (specs5/5-webapp/chat.md § File Mentions — "also collect file paths from
  // tool cards").
  const candidates = settled
    ? [...new Set([...(panel?.repoFiles || []), ...collectToolPaths(list)])]
    : [];

  const todos = latestTodos(list);
  const entries = groupBlocksByScope(list, options.subagents);

  return html`
    <div class="turn-blocks">
      ${entries.map((entry) => (entry.type === 'agent'
        ? renderSubagentRow(panel, entry.row, entry.blocks, candidates, settled)
        : renderBlock(panel, entry.block, candidates, settled)))}
      ${todos ? renderTodoList(todos, !settled) : nothing}
    </div>
  `;
}

/**
 * One block, dispatched on kind. Superseded blocks (every `TodoWrite` but the
 * last) draw nothing: their record stays in the list so block order does not
 * renumber, but the plan renders once, at the end of the turn.
 */
export function renderBlock(panel, block, candidates, settled) {
  if (!block || block.superseded) return nothing;
  if (block.kind === 'tool') {
    if (isTodoWrite(block.tool?.name)) return nothing;
    return renderToolCard(panel, block);
  }
  if (block.kind === 'thinking') return renderThinkingBlock(panel, block);
  return renderTextBlock(panel, block, candidates, settled);
}

// ---------------------------------------------------------------
// Text and thinking
// ---------------------------------------------------------------

export function renderTextBlock(panel, block, candidates, settled) {
  const content = typeof block?.content === 'string' ? block.content : '';
  if (!content) return nothing;
  let rendered = renderMarkdown(content);
  // A block that reported `done` will not change again even mid-turn, so its
  // mentions are safe to detect without waiting for the whole turn.
  if ((settled || block.done) && candidates?.length) {
    rendered = findFileMentions(rendered, candidates);
  }
  return html`
    <div class="md-content block-text" data-block-id=${block.block_id}>
      ${unsafeHTML(rendered)}
    </div>
  `;
}

/**
 * A thinking region: collapsed, above the text it precedes.
 *
 * Not labelled with a token count. specs5/5-webapp/chat.md asks for one, but
 * no payload carries per-block thinking tokens — `thinkingChunk` has
 * `{block_id, seq, content, done}` and the turn's `usage` is a single total
 * for the turn — so the label would have to be invented. Character counts
 * dressed up as tokens are worse than no number. The spec is corrected rather
 * than approximated.
 *
 * When the configured display mode is `omitted` no chunks arrive at all, so no
 * block exists and nothing draws — which is the spec's "not an empty one".
 */
export function renderThinkingBlock(panel, block) {
  const content = typeof block?.content === 'string' ? block.content : '';
  if (!content) return nothing;
  const expanded = blockExpanded(panel, block);
  return html`
    <div
      class="thinking-region ${expanded ? 'expanded' : ''}"
      data-block-id=${block.block_id}
    >
      <button
        class="thinking-toggle"
        aria-expanded=${expanded ? 'true' : 'false'}
        title="The agent's reasoning. Not included in copy or read-aloud."
        @click=${() => toggleBlock(panel, block)}
      >
        <span class="thinking-caret">${expanded ? '▾' : '▸'}</span>
        <span class="thinking-label">${block.done ? 'Thinking' : 'Thinking…'}</span>
      </button>
      ${expanded
        ? html`<div class="thinking-body">${content}</div>`
        : nothing}
    </div>
  `;
}

// ---------------------------------------------------------------
// Tool cards
// ---------------------------------------------------------------

const STATUS_GLYPH = {
  pending: '',
  awaiting: '🔒',
  ok: '',
  error: '',
  denied: '',
};

const STATUS_TITLE = {
  pending: 'Running',
  awaiting: 'Waiting for your permission',
  ok: 'Finished',
  error: 'Failed',
  denied: 'You denied this call',
};

export function renderToolCard(panel, block) {
  const card = block.tool || {};
  const status = toolStatus(block);
  const expanded = blockExpanded(panel, block);
  const result = block.result;
  const segments = diffSegments(block);
  const files = Array.isArray(result?.files_modified) ? result.files_modified : [];
  const duration = formatDuration(result?.duration_ms);

  return html`
    <div
      class="tool-card tool-status-${status}"
      data-block-id=${block.block_id}
      data-tool=${card.name || ''}
    >
      <button
        class="tool-header"
        aria-expanded=${expanded ? 'true' : 'false'}
        @click=${() => toggleBlock(panel, block)}
      >
        <span
          class="tool-dot status-${status}"
          title=${STATUS_TITLE[status] || status}
        >${STATUS_GLYPH[status] || ''}</span>
        ${card.server
          ? html`<span class="tool-server-chip" title="MCP server">${card.server}</span>`
          : nothing}
        <span class="tool-name">${toolLabel(card)}</span>
        <span class="tool-summary">${card.input_summary || ''}</span>
        ${block.gated
          ? html`<span
              class="tool-gated"
              title="This call went through a permission prompt"
            >gated</span>`
          : nothing}
        <span class="tool-caret">${expanded ? '▾' : '▸'}</span>
      </button>
      ${expanded ? renderToolBody(block, card, result, segments) : nothing}
      ${files.length || duration
        ? html`
            <div class="tool-footer">
              ${duration ? html`<span class="tool-duration">${duration}</span>` : nothing}
              ${files.map((path) => renderFileChip(path))}
            </div>
          `
        : nothing}
    </div>
  `;
}

/**
 * The denials nobody chose, and what to call them.
 *
 * These arrive with `resolved_by` set to the same word as the action, so the
 * generic "<action> by <resolver>" line rendered "shutdown by shutdown". None
 * of them is a person's decision, so none of them carries a "by".
 */
const MACHINE_DENIAL_LABELS = {
  timeout: 'Denied — no host client was connected to answer',
  cancelled: 'Denied — the turn ended before it was answered',
  shutdown: 'Denied — the session shut down',
};

/**
 * The card body: the denial reason if there was one, otherwise the input and
 * then the result.
 *
 * A denial replaces the input rather than sitting beside it. The call never
 * ran, so its input is a proposal rather than a record, and the reason the
 * user gave is the thing worth reading — the agent saw it too and acted on it.
 */
function renderToolBody(block, card, result, segments) {
  if (block.denial) {
    const machine = MACHINE_DENIAL_LABELS[block.denial.action];
    return html`
      <div class="tool-body tool-body-denied">
        <div class="tool-denial-label">
          ${machine || 'Denied'}
          ${!machine && block.denial.resolvedBy
            ? html` by ${block.denial.resolvedBy}`
            : nothing}
        </div>
        <div class="tool-denial-reason">
          ${block.denial.reason || 'No reason given.'}
        </div>
      </div>
    `;
  }
  return html`
    <div class="tool-body">
      ${segments.length
        ? segments.map((segment) => html`${unsafeHTML(renderEditBody(segment))}`)
        : renderToolInput(card)}
      ${result ? renderToolResult(result) : nothing}
    </div>
  `;
}

function renderToolInput(card) {
  const input = card?.input;
  if (!input || typeof input !== 'object' || Object.keys(input).length === 0) {
    return nothing;
  }
  let text;
  try {
    text = JSON.stringify(input, null, 2);
  } catch {
    // Circular or otherwise unserialisable input. The summary in the header
    // already survived the trip, so the body degrades rather than throwing
    // inside a render pass and taking the whole panel with it.
    text = String(input);
  }
  return html`<pre class="tool-input">${text}</pre>`;
}

/**
 * A tool result's preview.
 *
 * `truncated` gets a marker naming the full size, not a "show all" button.
 * The engine sends only the truncated preview — the untruncated text never
 * leaves the server — so a button would expand to the same content it already
 * showed. Saying how much was withheld is the honest version of that
 * affordance; the spec is corrected to match.
 */
function renderToolResult(result) {
  const preview = typeof result.preview === 'string' ? result.preview : '';
  const isError = result.status === 'error';
  return html`
    <div class="tool-result ${isError ? 'tool-result-error' : ''}">
      ${preview
        ? html`<pre class="tool-result-body">${preview}</pre>`
        : html`<div class="tool-result-empty">No output.</div>`}
      ${result.truncated
        ? html`
            <div class="tool-result-truncated">
              Truncated${Number.isFinite(result.full_bytes)
                ? html` — ${formatBytes(result.full_bytes)} in full`
                : nothing}. The engine sends a preview only.
            </div>
          `
        : nothing}
    </div>
  `;
}

/**
 * A clickable path. Navigating to the diff viewer is the point of the footer:
 * "what did it just do to my repo" is only useful if the answer is one click
 * from the diff.
 */
export function renderFileChip(path) {
  return html`
    <span
      class="tool-file-chip"
      title="Open ${path}"
      @click=${(event) => {
        event.stopPropagation();
        window.dispatchEvent(new CustomEvent('navigate-file', {
          detail: { path },
          bubbles: false,
        }));
      }}
    >${path}</span>
  `;
}

// ---------------------------------------------------------------
// Todo lists
// ---------------------------------------------------------------

const TODO_MARK = {
  completed: '☑',
  in_progress: '◐',
  pending: '☐',
};

/**
 * The live plan.
 *
 * One list per turn, not one per `TodoWrite` call — `blocks.js` supersedes the
 * earlier calls so a fifteen-step turn shows its plan rather than fifteen
 * snapshots of it. An in-progress item prefers `activeForm` ("Running the
 * tests") over `content` ("Run the tests"), matching what the terminal shows
 * and reading correctly for the step actually happening.
 */
export function renderTodoList(todos, live) {
  const items = Array.isArray(todos) ? todos : [];
  if (items.length === 0) return nothing;
  const done = items.filter((item) => item.status === 'completed').length;
  return html`
    <div class="todo-list ${live ? 'live' : 'final'}">
      <div class="todo-header">
        <span class="todo-title">Plan</span>
        <span class="todo-count">${done}/${items.length}</span>
      </div>
      <ul class="todo-items">
        ${items.map((item) => {
          const status = typeof item.status === 'string' ? item.status : 'pending';
          const text = status === 'in_progress' && item.activeForm
            ? item.activeForm
            : item.content;
          return html`
            <li class="todo-item todo-${status}">
              <span class="todo-mark">${TODO_MARK[status] || '☐'}</span>
              <span class="todo-text">${text || ''}</span>
            </li>
          `;
        })}
      </ul>
    </div>
  `;
}

// ---------------------------------------------------------------
// Subagent rows
// ---------------------------------------------------------------

/**
 * The human label for a subagent — "explore: find auth call sites".
 *
 * Per specs5/5-webapp/subagent-browser.md § Tab Strip: the type plus the
 * description, because an SDK agent id is opaque and means nothing to a
 * reader. Two vocabularies reach here — a live row's `task_type` and a
 * transcript listing's `agent_type` — and either part may be missing;
 * a row with neither returns '' and its caller falls back to the id.
 */
export function subagentLabel(row) {
  const rawType = row?.task_type || row?.agent_type;
  const type = typeof rawType === 'string' ? rawType.trim() : '';
  const desc =
    typeof row?.description === 'string' ? row.description.trim() : '';
  if (type && desc) return `${type}: ${desc}`;
  return desc || type || '';
}

/**
 * Ask for one subagent's transcript in a tab of its own.
 *
 * The row is the evidence the subagent ran, so it is also the way in to
 * what it did (specs5/5-webapp/chat.md § Subagent Activity). Dispatched
 * rather than called: `tabs.js` owns the strip, and the panel is listening.
 */
function openSubagentTranscript(panel, row) {
  panel?.dispatchEvent(
    new CustomEvent('view-subagents-requested', {
      detail: {
        agents: [{ agent_id: row.agent_id, label: subagentLabel(row) }],
      },
      bubbles: true,
      composed: true,
    }),
  );
}

/**
 * A subagent's row, with its tool cards indented beneath it.
 *
 * The row spins until its status is terminal. `terminal` latches from either
 * an explicit terminal status or a notification, because a task can reach a
 * terminal state with no notification at all — `stop_task` reports `killed`
 * that way — and a row that waited for one would spin forever
 * (specs5/5-webapp/chat.md § Subagent Activity).
 *
 * Stop is the only write affordance. AIC⚡DC did not create this subagent and
 * cannot message it; the one thing a user can legitimately do is end it.
 *
 * The description doubles as the way into the transcript, for a row that
 * names an agent. A button rather than a click handler on the row itself:
 * the row contains the subagent's own tool cards, which expand on click,
 * and one of those clicks must not also open a tab.
 */
export function renderSubagentRow(panel, row, blocks, candidates, settled) {
  if (!row) return nothing;
  const live = !row.terminal && !settled;
  // `TaskUsage`, not the per-model token counters: a task reports
  // `{total_tokens, tool_uses, duration_ms}`, which shares no field name with
  // them, so reading it with `turnTokens` scored every subagent at zero and
  // the chip below never drew. See `taskUsage` in turn-cost.js.
  const { tokens, toolUses } = taskUsage(row.usage);
  const desc = row.description || row.task_type || 'Subagent';
  return html`
    <div class="subagent-row ${live ? 'live' : 'terminal'}" data-agent-id=${row.agent_id || row.key}>
      <div class="subagent-head">
        <span class="subagent-dot ${live ? 'spinning' : ''}"></span>
        ${row.agent_id
          ? html`<button
              class="subagent-desc subagent-desc-button"
              title="Read this subagent's transcript"
              @click=${(e) => {
                e.stopPropagation();
                openSubagentTranscript(panel, row);
              }}
            >${desc}</button>`
          : html`<span class="subagent-desc">${desc}</span>`}
        ${row.task_type
          ? html`<span class="subagent-type">${row.task_type}</span>`
          : nothing}
        ${row.status
          ? html`<span class="subagent-status">${row.status}</span>`
          : nothing}
        ${row.last_tool_name
          ? html`<span class="subagent-tool">${row.last_tool_name}</span>`
          : nothing}
        ${toolUses > 0
          ? html`<span class="subagent-usage"
              >${toolUses} ${toolUses === 1 ? 'tool' : 'tools'}</span>`
          : nothing}
        ${tokens > 0
          ? html`<span class="subagent-usage">${formatTokens(tokens)} tok</span>`
          : nothing}
        ${live && row.task_id
          ? html`
              <button
                class="subagent-stop"
                title="Stop this subagent"
                @click=${() => panel?._stopSubagent?.(row)}
              >Stop</button>
            `
          : nothing}
      </div>
      ${row.summary
        ? html`<div class="subagent-summary">${row.summary}</div>`
        : nothing}
      ${blocks?.length
        ? html`
            <div class="subagent-blocks">
              ${blocks.map((block) => renderBlock(panel, block, candidates, settled))}
            </div>
          `
        : nothing}
    </div>
  `;
}

// ---------------------------------------------------------------
// Turn footer
// ---------------------------------------------------------------

/**
 * The turn footer, replacing the old edit-summary banner.
 *
 * Files modified comes first because it is the answer to "what did it just do
 * to my repo?" — everything else on the line is context for that. `files` is
 * passed in rather than derived here so the caller can union the engine's own
 * `files_modified` with the one recovered from the block list; a turn that
 * ended badly may have blocks the result message never summarised.
 *
 * @param {Object} panel
 * @param {Object} summary  the frozen `streamComplete` payload
 * @param {string[]} files
 */
export function renderTurnFooter(panel, summary, files) {
  if (!summary || typeof summary !== 'object') return nothing;
  const paths = Array.isArray(files) ? files : [];
  const toolCalls = Number.isFinite(summary.tool_calls) ? summary.tool_calls : 0;
  const prompts = Number.isFinite(summary.permission_prompts)
    ? summary.permission_prompts
    : 0;
  const duration = formatDuration(summary.duration_ms);
  const turns = Number.isFinite(summary.num_turns) ? summary.num_turns : null;
  // Both read the per-turn figures the engine derived for us, never the
  // cumulative `model_usage` / `total_cost_usd` sitting beside them: this
  // footer belongs to one turn, and a session running total on it would grow
  // on every turn while claiming to describe this one.
  const usage = modelUsageLines(summary);
  const cost = costLabel(summary);

  const stats = [];
  if (toolCalls > 0) {
    stats.push(html`
      <span class="turn-stat">
        ${toolCalls} tool ${toolCalls === 1 ? 'call' : 'calls'}${prompts > 0
          ? html`, ${prompts} asked`
          : nothing}
      </span>
    `);
  }
  if (duration) {
    stats.push(html`
      <span class="turn-stat">
        ${duration}${turns !== null ? html` · ${turns} engine ${turns === 1 ? 'turn' : 'turns'}` : nothing}
      </span>
    `);
  }
  for (const line of usage) {
    stats.push(renderUsageChip(line));
  }
  // Three renderings, and the tooltip says which: a price, "nothing extra"
  // for a turn the engine's total did not move for, and "cost unknown" when
  // the turn's share cannot be separated out. Nothing at all for a browsed
  // turn — cost is not in the CLI's transcript, and "unknown" on every
  // replayed footer would be noise about a thing that was never recorded.
  if (cost) {
    stats.push(html`
      <span
        class="turn-stat turn-cost ${cost.known ? '' : 'turn-cost-unknown'}"
        title=${cost.title}
      >${cost.text}</span>
    `);
  }

  if (paths.length === 0 && stats.length === 0 && !summary.mirror_gap) return nothing;

  return html`
    <div class="turn-footer">
      ${paths.length
        ? html`
            <div class="turn-files">
              <span class="turn-files-label">
                ${paths.length} ${paths.length === 1 ? 'file' : 'files'} modified
              </span>
              ${paths.map((path) => renderFileChip(path))}
            </div>
          `
        : nothing}
      ${stats.length ? html`<div class="turn-stats">${stats}</div>` : nothing}
      ${summary.mirror_gap
        ? html`
            <button
              class="turn-mirror-gap"
              @click=${() => revealHealth(panel)}
              title="This turn was not appended to the repo-local transcript. Click for engine health."
            >⚠ not mirrored to the local transcript</button>
          `
        : nothing}
    </div>
  `;
}
