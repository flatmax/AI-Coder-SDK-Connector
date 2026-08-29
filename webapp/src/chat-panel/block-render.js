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
import { toRepoPath } from '../repo-path.js';
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

/**
 * The `_blockExpansion` key for a subagent's nested tool cards.
 *
 * A row is not a block and has no `block_id`, but its key is a task id, which
 * is unique for the same reason a block id is — so the one Map serves both
 * without a second store or a tab-keyed lookup.
 */
function subagentBlocksKey(row) {
  const id = row?.key || row?.task_id || row?.agent_id || row?.tool_use_id;
  return id ? `subagent-blocks:${id}` : null;
}

/**
 * Whether a subagent's own tool cards are showing. Collapsed unless the user
 * said otherwise — the same transcript has a tab of its own, and drawing it
 * inline made a delegated turn read as though it had happened twice
 * (specs5/5-webapp/chat.md § Subagent Activity).
 *
 * No auto-expand branch, deliberately, and in particular not one for a live
 * subagent: the head already carries the running status and the tool the
 * subagent is in, which is the part worth watching from the main transcript.
 */
export function subagentBlocksExpanded(panel, row) {
  const key = subagentBlocksKey(row);
  return key ? panel?._blockExpansion?.get(key) === true : false;
}

/** Flip a subagent's nested cards and repaint. */
export function toggleSubagentBlocks(panel, row) {
  const key = subagentBlocksKey(row);
  if (!panel || !key) return;
  if (!panel._blockExpansion) panel._blockExpansion = new Map();
  panel._blockExpansion.set(key, !subagentBlocksExpanded(panel, row));
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

/**
 * A tool card's `invoked_at` as epoch milliseconds, or `null`.
 *
 * `null` for a card that carries no time at all — one replayed from a
 * transcript entry the CLI wrote without a `timestamp`, or from a session
 * recorded before the engine started sending the field. Absent stays absent:
 * the alternative, defaulting to "now", would put a fresh clock reading on a
 * call made last Tuesday and make an ancient card look like it had just
 * stalled. Unparseable is treated as absent for the same reason, following
 * `expiryMs` in ../permission-dialog/queue.js.
 *
 * @param {object|null|undefined} card
 * @returns {number|null}
 */
export function invokedAtMs(card) {
  const raw = card?.invoked_at;
  if (typeof raw === 'number' && Number.isFinite(raw)) return raw;
  if (typeof raw !== 'string' || !raw) return null;
  const parsed = Date.parse(raw);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * A tool card's invocation time, as a reader would say it.
 *
 * Time of day alone for a call made today, because that is the whole point of
 * the chip — you read "14:32:07", glance at your own clock, and you know
 * whether the call has been sitting there for eight minutes. Date *and* time
 * for anything older, because a bare "14:32:07" on a card replayed from last
 * week's session invites exactly the arithmetic that would be wrong.
 *
 * Locale formatting rather than a fixed pattern, matching the rest of the
 * webapp's timestamps (`toLocaleTimeString` in ../context-usage-tab.js). The
 * cost is that seconds are locale-dependent; every locale this runs in shows
 * them, and a chip without seconds would still answer the question.
 *
 * @param {number|null} ms
 * @returns {string}
 */
export function formatInvokedAt(ms) {
  if (!Number.isFinite(ms)) return '';
  try {
    const then = new Date(ms);
    return then.toDateString() === new Date().toDateString()
      ? then.toLocaleTimeString()
      : then.toLocaleString();
  } catch (_) {
    return '';
  }
}

/**
 * A byte count as a short string. The one owner of that rendering.
 *
 * Grew a GB tier when the Settings tab's session-storage figure started
 * calling it: a truncated tool result is a few hundred KB and stops at MB,
 * but `.aic-dc/sessions/` is warned about in gigabytes, and `1782.4 MB` is a
 * number a reader has to divide before it means anything. Same 1024 base
 * throughout, and the existing tiers' labels are left as they are — the
 * engine's own warning sentence says GiB for the same arithmetic, and one
 * function using two labelling conventions would be worse than the mismatch.
 */
export function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return '';
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) {
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
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

/**
 * The header's one-line rendering of a call's input, capped.
 *
 * The number is the header's whole defence against a card growing without
 * bound: `.tool-summary` carries no ellipsis and no line clamp on purpose
 * (see `styles.js`), because the header is the only place a collapsed card
 * says what the call was about. The cap is what makes that safe.
 */
export const TOOL_SUMMARY_CHARS = 200;

/**
 * `key=value` over a tool call's input, with repo paths shortened.
 *
 * Built here rather than shipped as a `input_summary` field off the engine,
 * which is what used to happen (`specs5/next.md` § C3). The reason is the
 * paths: the engine's join could not shorten them, so the header read
 * `file_path=/home/you/repo/src/a.js` and spent two or three of its rows on
 * a prefix identical for every file in the repo. `chat.md` recorded that as
 * blocked on needing "a per-tool table of path keys" — and **there is no
 * table**. A value that begins with the repo root is a path by its shape,
 * which is the same discriminator `repo-path.js` already mirrors off the
 * backend's own check, so `toRepoPath` can be handed every string value and
 * will decline the ones that are not paths. The card already carries its
 * full `input`, so moving the render here costs no extra field on the wire
 * and gives live cards and cards read back off a transcript one builder
 * instead of two.
 *
 * Two limits worth naming. A path *inside* a value is left alone — the rule
 * renames a path, it does not rewrite prose, so an `old_string` quoting an
 * absolute path keeps it. And a value that merely starts with the root
 * without being a path (a shell command, say) is shortened too, which is a
 * gain rather than a cost: the prefix is noise either way.
 *
 * Non-string values are JSON, as they were on the server. `JSON.stringify`
 * writes `[1,2]` where Python's `json.dumps` wrote `[1, 2]`; that one space
 * is the only rendering this move changes. It is also the one call in here
 * that can throw, so it is caught per value rather than per summary: a single
 * circular value costs the header that one part and leaves the others, where
 * letting the throw out would cost the whole panel the render pass.
 */
export function toolInputSummary(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) return '';
  const parts = [];
  for (const [key, value] of Object.entries(input)) {
    let rendered;
    if (typeof value === 'string') {
      rendered = toRepoPath(value);
    } else {
      try {
        rendered = JSON.stringify(value);
      } catch {
        rendered = String(value);
      }
    }
    // Collapsed per part, then joined: a `Bash` heredoc arrives with its
    // newlines in it and a header row is one line.
    parts.push(`${key}=${String(rendered ?? '')}`.trim().split(/\s+/).join(' '));
  }
  const summary = parts.join(' ');
  if (summary.length > TOOL_SUMMARY_CHARS) {
    return `${summary.slice(0, TOOL_SUMMARY_CHARS - 1)}…`;
  }
  return summary;
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

/**
 * The two statuses that mean the call has not finished, and so the two where
 * "how long has this been going?" is a question worth answering.
 *
 * `denied` is not one of them. A denied call never ran, so time since it was
 * proposed measures how long the user took to say no — a fact about the
 * reader, rendered as though it were a fact about the tool.
 */
const RUNNING_STATUSES = new Set(['pending', 'awaiting']);

/**
 * The header's time chip: when the call was made, and — while it is still
 * running — how long ago that was.
 *
 * The invocation time is the durable half and always renders when known. The
 * elapsed half is live, and renders *only while the panel's run-timer ticker
 * is running*, because that interval is the one thing guaranteeing the number
 * gets recomputed. Without the ticker a rendered "2m 41s" freezes at whatever
 * it said on the last re-render and goes on claiming to be live, which is
 * worse than the clock time alone — the reader can always do the subtraction
 * themselves, and the wall clock does not stop.
 *
 * Elapsed is clamped at zero. It subtracts the *engine's* clock reading from
 * the *browser's*, so a skew between two machines — or an NTP correction on
 * either — can otherwise produce a call that has been running for minus four
 * seconds. Same reasoning as `_elapsed_ms` in the engine's history.py.
 *
 * The two halves stack rather than sitting on one line joined by a middle dot.
 * The chip lives in the header's metadata rail now, and a rail is a narrow
 * column: `12:29:29 PM · 2m 41s` wants all of one and a restored card's
 * `Aug 27, 12:29:29 PM · 2m 41s` wants more, so the dot version wrapped to
 * `12:29:29 PM ·` with the separator dangling at the end of a line. Stacked,
 * each half gets a line of a column that has lines to spare.
 */
function renderToolTime(panel, status, card) {
  const invokedMs = invokedAtMs(card);
  const clock = formatInvokedAt(invokedMs);
  if (!clock) return nothing;
  const ticking = RUNNING_STATUSES.has(status) && panel?._streamTimerInterval != null;
  const elapsed = ticking ? formatDuration(Math.max(0, Date.now() - invokedMs)) : '';
  const title = elapsed
    ? `Invoked at ${clock} by the engine's clock — running for ${elapsed}`
    : `Invoked at ${clock} by the engine's clock`;
  // The space between the two spans is deliberate. They stack, so it draws
  // nothing — whitespace between flex items is discarded — but it is the
  // only thing keeping "14:32:07" and "2m 41s" from running together into
  // one word for a screen reader, or for anything else reading text content.
  return html`
    <span class="tool-time" title=${title}>
      <span class="tool-clock">${clock}</span>
      ${elapsed ? html`<span class="tool-elapsed">${elapsed}</span>` : nothing}
    </span>
  `;
}

export function renderToolCard(panel, block) {
  const card = block.tool || {};
  const status = toolStatus(block);
  const expanded = blockExpanded(panel, block);
  const result = block.result;
  const segments = diffSegments(block);
  const files = Array.isArray(result?.files_modified) ? result.files_modified : [];
  const duration = formatDuration(result?.duration_ms);
  // Hoisted because the rail draws a line for it or draws no line at all —
  // a card with no `invoked_at`, which is every card written before the
  // field existed, would otherwise carry an empty row.
  const timeChip = renderToolTime(panel, status, card);

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
        <!-- The header is two columns, and this is the left one: what the
             call *is* — status, server, name, when it was made, whether it
             was gated — with the summary alone in the column beside it.
             Nothing is pinned to the right edge any more. A right-hand
             group was subtracted from every line of the summary rather
             than only the first: the summary is one box, and a box that
             stops short of the time chip stops short of it all the way
             down.

             The caret leads, as it does on a thinking region's toggle. It
             is also the one thing here that cannot afford to wrap onto a
             line of its own, and first is the position where it can't.

             A line each, rather than the time and the gated marker sharing
             one. Together they wanted all of the rail and a little more on
             a card whose clock carries a date, so the marker wrapped under
             the clock on some cards and sat beside it on others — which is
             the reader having to look in two places for the same fact.
             Rail lines are the cheap thing here; the summary beside them
             is usually taller anyway. -->
        <span class="tool-meta">
          <span class="tool-meta-line">
            <span class="tool-caret">${expanded ? '▾' : '▸'}</span>
            <span
              class="tool-dot status-${status}"
              title=${STATUS_TITLE[status] || status}
            >${STATUS_GLYPH[status] || ''}</span>
            ${card.server
              ? html`<span class="tool-server-chip" title="MCP server">${card.server}</span>`
              : nothing}
            <span class="tool-name">${toolLabel(card)}</span>
          </span>
          ${timeChip !== nothing ? html`<span class="tool-meta-line">${timeChip}</span>` : nothing}
          ${block.gated
            ? html`<span class="tool-meta-line"
                ><span
                  class="tool-gated"
                  title="This call went through a permission prompt"
                >gated</span></span
              >`
            : nothing}
        </span>
        <span class="tool-summary">${toolInputSummary(card.input)}</span>
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
    // Circular or otherwise unserialisable input. The body degrades rather
    // than throwing inside a render pass and taking the whole panel with it.
    // The header used to be the reason this was cheap — it arrived
    // pre-joined off the engine and so could not fail. It is built here now
    // (`toolInputSummary`, § C3) and catches the same throw per value, so
    // both halves of the card degrade on their own.
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
 *
 * **The label is repo-relative; the tooltip keeps the absolute path.** Every
 * path on a tool card is absolute, because Claude Code's file tools require
 * that, and an absolute path is the wrong label for a chip: it spends its
 * width on a prefix that is the same for every file in the repo, and the part
 * that identifies the file is the part that falls off the end. This is the
 * house rule the Context tab's memory-file table already states — named
 * relative to the root, engine's path on the tooltip — applied here
 * (next.md § C4).
 *
 * The `detail.path` stays whatever it was. `onNavigateFile` normalises, has
 * done since the click was fixed, and is the one place that should: a second
 * conversion here would be a second thing to keep true, and the label is a
 * display concern that must not become the navigation contract.
 */
export function renderFileChip(path) {
  const label = toRepoPath(path);
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
    >${label}</span>
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
 * reader. Two vocabularies reach here — a live row's `subagent_type` and a
 * transcript listing's `agent_type` — and either part may be missing;
 * a row with neither returns '' and its caller falls back to the id.
 *
 * Not `task_type`: that names the transport ("local_agent"), so labelling
 * with it produced "local_agent: find auth call sites" on every row alike.
 */
export function subagentLabel(row) {
  const rawType = row?.subagent_type || row?.agent_type;
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
 *
 * What the row shows is a decision about *duplication*, because a delegated
 * turn describes the same subagent four times over: the `Task` card's own
 * header, this row's head, this summary, and usually the agent's prose above
 * it. Two of those are dropped here. The nested cards collapse, because the
 * subagent's transcript already has a tab and drawing it inline is a second
 * copy of it. The type chip shows `subagent_type` and not `task_type`, which
 * is the transport kind and reads "local_agent" on every row alike.
 *
 * `summary` survives, and is the one line that earns its place: a headless CLI
 * capture on 2026-08-25 showed it is the subagent's own closing answer
 * ("The magic word is **ORCHID**"), not a restatement of the description — so
 * collapsing it away would lose the only record of what the subagent
 * *concluded*. It arrives as markdown and is rendered as such, through the
 * same escaping path as message text.
 */
export function renderSubagentRow(panel, row, blocks, candidates, settled) {
  if (!row) return nothing;
  const live = !row.terminal && !settled;
  // `TaskUsage`, not the per-model token counters: a task reports
  // `{total_tokens, tool_uses, duration_ms}`, which shares no field name with
  // them, so reading it with `turnTokens` scored every subagent at zero and
  // the chip below never drew. See `taskUsage` in turn-cost.js.
  const { tokens, toolUses } = taskUsage(row.usage);
  const desc = row.description || row.subagent_type || 'Subagent';
  // What the disclosure would actually draw, which is not `blocks.length`:
  // a subagent's nested blocks are its prose as well as its calls, and
  // `renderBlock` draws nothing for a superseded block or a `TodoWrite`. A
  // count taken before that filter said "3 tool calls" over one `Read`.
  const nested = (blocks || []).filter((b) => b && !b.superseded);
  const calls = nested.filter(
    (b) => b.kind === 'tool' && !isTodoWrite(b.tool?.name),
  ).length;
  // The prose comes with the calls either way, so the count leads only when
  // there is one to give; a subagent that just answered gets the plain noun.
  const disclosure = calls > 0
    ? `${calls} ${calls === 1 ? 'tool call' : 'tool calls'}`
    : 'transcript';
  const showBlocks = nested.length > 0 && subagentBlocksExpanded(panel, row);
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
        ${row.subagent_type
          ? html`<span class="subagent-type">${row.subagent_type}</span>`
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
        ? html`<div class="subagent-summary md-content">
            ${unsafeHTML(renderMarkdown(row.summary))}
          </div>`
        : nothing}
      ${nested.length > 0
        ? html`
            <button
              class="subagent-blocks-toggle"
              aria-expanded=${showBlocks ? 'true' : 'false'}
              title="Show this subagent's own transcript inline"
              @click=${(e) => {
                e.stopPropagation();
                toggleSubagentBlocks(panel, row);
              }}
            >
              <span class="subagent-caret">${showBlocks ? '▾' : '▸'}</span>
              ${disclosure}
            </button>
          `
        : nothing}
      ${showBlocks
        ? html`
            <div class="subagent-blocks">
              ${nested.map((block) => renderBlock(panel, block, candidates, settled))}
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
