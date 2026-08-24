// Block-keyed turn state for the Claude Code message stream.
//
// A turn is no longer "one assistant message that accumulates text". It is a
// sequence of blocks — text, thinking, tool calls with their results —
// produced in arrival order, each identified and updated independently
// (specs5/5-webapp/chat.md § Block-Keyed Rendering).
//
// The old contract was that any single chunk could rebuild the whole view,
// because every chunk carried the entire accumulated turn. The replacement is
// weaker but bounded: any single chunk can rebuild *its block*. That is why
// block identity is threaded through every function here rather than treated
// as an optimisation — a renderer that keys on array position instead would
// reorder blocks the moment a subagent interleaves with the main scope.
//
// Everything in this module is a plain function over plain objects. The tab
// owns the state; `streaming.js` owns when to mutate it; `block-render.js`
// owns how it looks. No Lit, no RPC, no DOM.
//
// Two identity rules that the rest of the frontend depends on:
//
//   - Text and thinking blocks are keyed by the engine's `{request_id}:b{n}`
//     block id.
//   - Tool blocks are keyed by the SDK's own `tool_use_id`, because the
//     result references it. No correlation table on either side.

// The one import: a plain array of protocol strings, no side effects, so the
// module stays a pile of pure functions. It is imported rather than repeated
// because the dialog and this file have to agree on which decisions let a
// call through, and when they disagreed the transcript called an approved
// call denied.
import { ALLOW_ACTIONS } from '../permission-dialog/constants.js';

/**
 * Fresh per-turn block state.
 *
 * `blocks` is the ordered render list. `index` maps block id → the same
 * record objects, so an update is a Map lookup rather than a scan; the two
 * are always kept in step and neither is authoritative on its own.
 *
 * `pending` is the rAF staging area: block id → the latest chunk payload not
 * yet applied. `subagents` is agent id → its row.
 *
 * `usage` is the turn's running token counters — the latest `turnUsage`
 * payload, replaced rather than accumulated because the engine sends the total
 * and not a delta. Null until the first assistant message reports one, which is
 * why the streaming card draws no counter at all for the first few seconds.
 */
export function makeTurnBlocks() {
  return {
    blocks: [],
    index: new Map(),
    pending: new Map(),
    subagents: new Map(),
    usage: null,
  };
}

/**
 * Reset a turn's block state in place.
 *
 * Called when a turn settles (its blocks having been frozen onto the message
 * first) and when a session is replaced. Mutates rather than replacing so
 * every holder of the object — the tab, an in-flight rAF closure — sees the
 * same empty state.
 */
export function resetTurnBlocks(turn) {
  if (!turn) return;
  turn.blocks.length = 0;
  turn.index.clear();
  turn.pending.clear();
  turn.subagents.clear();
  // The next turn starts from no counter rather than from the last one's
  // total. It is dropped rather than carried onto the settled message
  // because the result message's own `turn_model_usage` is already there and
  // is the authoritative version of the same figure.
  turn.usage = null;
}

/**
 * Freeze a turn's blocks for storage on a settled message.
 *
 * Shallow-copies each record so later mutation of the live turn — a late tool
 * result, the next turn reusing the state object — cannot rewrite history
 * that the user has already read. Tool cards and results are copied one level
 * deeper for the same reason.
 */
export function freezeBlocks(turn) {
  if (!turn) return [];
  return turn.blocks.map((block) => ({
    ...block,
    ...(block.tool ? { tool: { ...block.tool } } : {}),
    ...(block.result ? { result: { ...block.result } } : {}),
  }));
}

// ---------------------------------------------------------------
// Text and thinking chunks
// ---------------------------------------------------------------

/**
 * Stage a `streamChunk` / `thinkingChunk` payload for the next frame.
 *
 * Returns true when the chunk is worth a re-render and false when it was
 * discarded. Discard rules, in order:
 *
 *   1. No `block_id` — unroutable. The engine always sends one; a payload
 *      without one is a protocol break, not a block to invent.
 *   2. Stale `seq` — not greater than the highest already *seen* for this
 *      block, counting staged-but-unapplied chunks. Within a block, drops
 *      and reorderings are harmless because content is cumulative; across
 *      blocks, arrival order is the contract.
 *
 * Staging (rather than writing straight through) is what lets rapid-fire
 * chunks coalesce: the frame callback drains one entry per block no matter
 * how many arrived. `seq` is tracked on the staged entry too, so a burst of
 * out-of-order chunks inside one frame still lands the highest one.
 */
export function stageChunk(turn, payload, kind) {
  if (!turn || !payload || typeof payload !== 'object') return false;
  const blockId = payload.block_id;
  if (typeof blockId !== 'string' || !blockId) return false;
  const seq = Number.isFinite(payload.seq) ? payload.seq : 0;
  const highest = highestSeq(turn, blockId);
  if (highest !== null && seq <= highest) return false;
  turn.pending.set(blockId, {
    kind: kind === 'thinking' ? 'thinking' : 'text',
    seq,
    content: typeof payload.content === 'string' ? payload.content : '',
    done: !!payload.done,
    agentId: normalizeAgentId(payload.agent_id),
  });
  return true;
}

/**
 * Highest `seq` known for a block — staged or applied, whichever is further
 * ahead. Null when the block has never been seen, which is how `stageChunk`
 * distinguishes "first chunk" from "stale chunk with seq 0".
 */
function highestSeq(turn, blockId) {
  const staged = turn.pending.get(blockId);
  const applied = turn.index.get(blockId);
  if (staged === undefined && applied === undefined) return null;
  const a = staged ? staged.seq : -1;
  const b = applied && Number.isFinite(applied.seq) ? applied.seq : -1;
  return Math.max(a, b);
}

/**
 * Drain every staged chunk into its block. Returns true when anything
 * changed, so the caller can skip a `requestUpdate` on an empty frame.
 *
 * A staged chunk for an unknown block appends a new record — that is the
 * only place a text or thinking block comes into existence, so arrival order
 * of first-chunks is render order.
 */
export function drainChunks(turn) {
  if (!turn || turn.pending.size === 0) return false;
  let changed = false;
  for (const [blockId, staged] of turn.pending) {
    const existing = turn.index.get(blockId);
    if (existing === undefined) {
      appendBlock(turn, {
        block_id: blockId,
        kind: staged.kind,
        seq: staged.seq,
        content: staged.content,
        done: staged.done,
        agent_id: staged.agentId,
      });
      changed = true;
      continue;
    }
    if (Number.isFinite(existing.seq) && staged.seq <= existing.seq) {
      // Superseded between staging and draining. Nothing to apply; the
      // block already holds content at least as new.
      continue;
    }
    existing.seq = staged.seq;
    existing.content = staged.content;
    existing.done = staged.done;
    changed = true;
  }
  turn.pending.clear();
  return changed;
}

// ---------------------------------------------------------------
// Tool blocks
// ---------------------------------------------------------------

/**
 * Add (or refresh) a tool card.
 *
 * Idempotent by `tool_use_id`: the same assistant message can be
 * re-delivered, and the engine already de-duplicates, but a reconnect replay
 * followed by a live event is a legitimate second delivery. The second one
 * updates the card rather than appending a twin.
 *
 * A `TodoWrite` call supersedes every earlier one in the turn, so a long turn
 * shows one live plan instead of fifteen snapshots
 * (specs5/5-webapp/chat.md § Todo Lists). The superseded cards stay in the
 * list — dropping them would renumber block order — and the renderer skips
 * them.
 */
export function applyToolUse(turn, card) {
  if (!turn || !card || typeof card !== 'object') return false;
  const toolUseId = card.tool_use_id;
  if (typeof toolUseId !== 'string' || !toolUseId) return false;
  const existing = turn.index.get(toolUseId);
  if (existing !== undefined) {
    existing.tool = { ...existing.tool, ...card };
    existing.agent_id = normalizeAgentId(card.agent_id) ?? existing.agent_id;
    return true;
  }
  if (isTodoWrite(card.name)) {
    for (const block of turn.blocks) {
      if (block.kind === 'tool' && isTodoWrite(block.tool?.name)) {
        block.superseded = true;
      }
    }
  }
  appendBlock(turn, {
    block_id: toolUseId,
    kind: 'tool',
    seq: 0,
    content: '',
    done: false,
    agent_id: normalizeAgentId(card.agent_id),
    tool: { ...card },
    result: null,
    // Set by `markAwaitingPermission` when a dialog opens for this call, and
    // left set afterwards: the transcript records that the user authorised
    // it, which is the point of the gated marker.
    gated: !!card.gated,
    denial: null,
    superseded: false,
  });
  return true;
}

/**
 * Attach a tool result to its card by `tool_use_id`.
 *
 * A result for a card we never saw is dropped rather than rendered on its
 * own. The engine logs that case loudly; a floating result with no call to
 * attach it to would render as a card with no header, which reads as a
 * rendering bug rather than a missed message.
 */
export function applyToolResult(turn, payload) {
  if (!turn || !payload || typeof payload !== 'object') return false;
  const toolUseId = payload.tool_use_id;
  if (typeof toolUseId !== 'string' || !toolUseId) return false;
  const block = turn.index.get(toolUseId);
  if (block === undefined || block.kind !== 'tool') return false;
  block.result = { ...payload };
  block.done = true;
  block.tool = {
    ...block.tool,
    status: payload.status === 'error' ? 'error' : 'ok',
  };
  return true;
}

/**
 * Flag a tool call as waiting on the permission dialog.
 *
 * The amber lock is the honest state for a call that is neither in flight nor
 * finished: the engine is blocked on a human. Called from the
 * `permissionRequest` broadcast, which carries the `tool_use_id`.
 *
 * The call may not have a card yet — the control request can beat the
 * assistant message that describes it — in which case there is nothing to
 * mark and the card arrives already `gated` from the engine.
 */
export function markAwaitingPermission(turn, toolUseId) {
  if (!turn || typeof toolUseId !== 'string' || !toolUseId) return false;
  const block = turn.index.get(toolUseId);
  if (block === undefined || block.kind !== 'tool') return false;
  block.gated = true;
  block.awaiting = true;
  return true;
}

/**
 * Record how a permission request for a tool call was resolved.
 *
 * Any of the allow actions clears the amber lock and lets the call go back to
 * pending — the engine is running it now. Everything else (deny, timeout,
 * shutdown) is a denial, and the reason is rendered as the card's body because
 * the agent saw that same reason and will act on it.
 *
 * The test is against `ALLOW_ACTIONS`, not `=== 'allow'`. "Always allow" and
 * the session mode switch both let the call through while reporting their own
 * action name, and treating either as a denial marks a card the user approved
 * as refused.
 */
export function applyPermissionOutcome(turn, payload) {
  if (!turn || !payload || typeof payload !== 'object') return false;
  const toolUseId = payload.tool_use_id;
  if (typeof toolUseId !== 'string' || !toolUseId) return false;
  const block = turn.index.get(toolUseId);
  if (block === undefined || block.kind !== 'tool') return false;
  block.awaiting = false;
  block.gated = true;
  if (ALLOW_ACTIONS.includes(payload.action)) {
    block.denial = null;
    return true;
  }
  block.denial = {
    action: typeof payload.action === 'string' ? payload.action : 'deny',
    reason: typeof payload.reason === 'string' ? payload.reason : '',
    resolvedBy:
      typeof payload.resolved_by === 'string' ? payload.resolved_by : '',
  };
  return true;
}

/**
 * The rendered status of a tool block, as the status table in
 * specs5/5-webapp/chat.md § Status defines it.
 *
 * Precedence is deliberate: a denial outranks whatever the card's own status
 * field says, because a denied call also produces an error-shaped tool result
 * and "error" would hide the fact that the user caused it.
 */
export function toolStatus(block) {
  if (!block || block.kind !== 'tool') return 'pending';
  if (block.denial) return 'denied';
  if (block.awaiting) return 'awaiting';
  const status = block.result?.status || block.tool?.status;
  if (status === 'error') return 'error';
  if (status === 'ok') return 'ok';
  return 'pending';
}

// ---------------------------------------------------------------
// Subagent rows
// ---------------------------------------------------------------

/**
 * Fold a `subagentEvent` into its row.
 *
 * Rows are keyed by `task_id`, which every one of the four `Task*` messages
 * carries. `agent_id` is the transcript key and the CLI reports it in the
 * message payload rather than the dataclass, so it can be absent on an event
 * that arrives first — keying on it would open a second row for the same task
 * the moment one showed up. Fields arrive piecemeal across the four message
 * types, so each event patches the row rather than replacing it: a `progress`
 * event with no description must not blank the description a `started` event
 * supplied.
 *
 * A task can reach a terminal status through `updated` with no notification
 * at all (`stop_task` reports `killed` that way), so `terminal` latches from
 * either message and the row stops spinning without waiting for one.
 */
export function applySubagentEvent(turn, payload) {
  if (!turn || !payload || typeof payload !== 'object') return false;
  const key = rowKey(payload);
  if (!key) return false;
  const existing = turn.subagents.get(key) || {
    key,
    agent_id: normalizeAgentId(payload.agent_id),
    task_id: payload.task_id || null,
    // The `Task` call that spawned this subagent. Its block id, so the
    // renderer can nest the row directly under the card that started it
    // instead of collecting subagents into a separate section.
    tool_use_id: payload.tool_use_id || null,
    description: '',
    // The transport kind ("local_agent") and the agent's own kind
    // ("Explore") respectively. Only the second is worth showing; see
    // `subagent_type` in `_task_event` (src/aic_dc/claude_code/messages.py).
    task_type: null,
    subagent_type: null,
    status: null,
    last_tool_name: null,
    usage: null,
    summary: null,
    terminal: false,
  };
  const patched = { ...existing };
  if (payload.agent_id) patched.agent_id = payload.agent_id;
  if (payload.task_id) patched.task_id = payload.task_id;
  if (payload.tool_use_id) patched.tool_use_id = payload.tool_use_id;
  if (payload.description) patched.description = payload.description;
  if (payload.task_type) patched.task_type = payload.task_type;
  if (payload.subagent_type) patched.subagent_type = payload.subagent_type;
  if (payload.status) patched.status = payload.status;
  if (payload.last_tool_name) patched.last_tool_name = payload.last_tool_name;
  if (payload.usage) patched.usage = payload.usage;
  if (payload.summary) patched.summary = payload.summary;
  if (payload.terminal) patched.terminal = true;
  turn.subagents.set(key, patched);
  return true;
}

function rowKey(payload) {
  for (const key of ['task_id', 'agent_id', 'tool_use_id']) {
    const value = payload[key];
    if (typeof value === 'string' && value) return value;
  }
  return null;
}

/**
 * The row a `subagentEvent` payload belongs to, after it has been folded in.
 *
 * `applySubagentEvent` answers "did anything change?", which is all a repaint
 * needs; a caller that has to *act* on the subagent — open its tab, label it,
 * decide its LED — needs the accumulated row rather than the one event's
 * fields. Split rather than returned from the fold because the row object is
 * replaced on every event (patched into a fresh object), so the only safe way
 * to hold one is to look it up again.
 */
export function subagentRowFor(turn, payload) {
  if (!turn || !payload || typeof payload !== 'object') return null;
  const key = rowKey(payload);
  if (!key) return null;
  return turn.subagents.get(key) || null;
}

// ---------------------------------------------------------------
// Reconnect replay
// ---------------------------------------------------------------

/**
 * Rebuild a turn's blocks from an `active_streams[].blocks` snapshot.
 *
 * Replay is block state, not a chunk log: superseded thinking content and
 * intermediate renderings are gone, and this must not imply otherwise
 * (specs5/5-webapp/chat.md § Reconnect Replay). What it does guarantee is
 * that a user who refreshed mid-turn sees the turn as it stands rather than
 * an empty card waiting for the next token.
 *
 * The snapshot's `seq` is adopted as-is so the next live chunk for a block
 * is compared against a real high-water mark. Its records carry no `done`
 * flag — a mid-turn snapshot is by definition unfinished — so blocks are
 * marked done only where the payload proves it: a tool card with a result.
 */
export function applyReplayBlocks(turn, blocks) {
  if (!turn || !Array.isArray(blocks)) return false;
  resetTurnBlocks(turn);
  let seenTodo = false;
  // Latest TodoWrite wins, so walk backwards to find it and supersede the
  // rest — the same rule live arrival applies, resolved in one pass.
  for (let i = blocks.length - 1; i >= 0; i -= 1) {
    const raw = blocks[i];
    if (!raw || typeof raw !== 'object') continue;
    if (raw.kind !== 'tool' || !isTodoWrite(raw.tool?.name)) continue;
    if (seenTodo) raw.__superseded = true;
    seenTodo = true;
  }
  for (const raw of blocks) {
    if (!raw || typeof raw !== 'object') continue;
    const blockId = raw.block_id;
    if (typeof blockId !== 'string' || !blockId) continue;
    if (turn.index.has(blockId)) continue;
    const kind = raw.kind === 'thinking' || raw.kind === 'tool'
      ? raw.kind
      : 'text';
    if (kind === 'tool') {
      const card = raw.tool && typeof raw.tool === 'object' ? raw.tool : {};
      const result = card.result && typeof card.result === 'object'
        ? card.result
        : null;
      appendBlock(turn, {
        block_id: blockId,
        kind: 'tool',
        seq: Number.isFinite(raw.seq) ? raw.seq : 0,
        content: '',
        done: !!result,
        agent_id: normalizeAgentId(card.agent_id),
        tool: { ...card, tool_use_id: card.tool_use_id || blockId },
        result,
        gated: !!card.gated,
        denial: null,
        superseded: !!raw.__superseded,
      });
      continue;
    }
    appendBlock(turn, {
      block_id: blockId,
      kind,
      seq: Number.isFinite(raw.seq) ? raw.seq : 0,
      content: typeof raw.content === 'string' ? raw.content : '',
      done: false,
      // A subagent narrates in text, not just tool calls, so the scope has to
      // survive replay: it is what puts the text under the subagent's row and
      // in the subagent's own tab rather than at turn level.
      agent_id: normalizeAgentId(raw.agent_id),
    });
  }
  return turn.blocks.length > 0;
}

// ---------------------------------------------------------------
// Derived views
// ---------------------------------------------------------------

/**
 * Every file this turn's tool calls modified, in first-seen order.
 *
 * The turn footer's most important line, and the answer to "what did it just
 * do to my repo?". Deduplicated because a file edited three times is one file
 * to look at. Falls back to the result payloads rather than trusting the
 * footer's own list, so a partially-rendered turn still names what it wrote.
 */
export function collectFilesModified(blocks) {
  const seen = new Set();
  const out = [];
  for (const block of Array.isArray(blocks) ? blocks : []) {
    const files = block?.result?.files_modified;
    if (!Array.isArray(files)) continue;
    for (const path of files) {
      if (typeof path !== 'string' || !path || seen.has(path)) continue;
      seen.add(path);
      out.push(path);
    }
  }
  return out;
}

/**
 * Paths a turn's tool calls touched, whether or not they changed anything —
 * the union of the modified files and the path-shaped values in each call's
 * input. Feeds the file-mention pass, which wants "what is this turn about?"
 * rather than "what did it write?".
 */
export function collectToolPaths(blocks) {
  const seen = new Set();
  for (const path of collectFilesModified(blocks)) seen.add(path);
  for (const block of Array.isArray(blocks) ? blocks : []) {
    if (block?.kind !== 'tool') continue;
    const input = block.tool?.input;
    if (!input || typeof input !== 'object') continue;
    for (const key of ['file_path', 'path', 'notebook_path']) {
      const value = input[key];
      if (typeof value === 'string' && value) seen.add(value);
    }
  }
  return [...seen];
}

/**
 * The live todo list for a turn: the newest `TodoWrite` call's items.
 *
 * Returns null when the turn has no todo call, which is the common case — a
 * turn short enough not to need a plan should not grow an empty checklist.
 */
export function latestTodos(blocks) {
  for (let i = (Array.isArray(blocks) ? blocks.length : 0) - 1; i >= 0; i -= 1) {
    const block = blocks[i];
    if (block?.kind !== 'tool' || !isTodoWrite(block.tool?.name)) continue;
    const todos = block.tool?.input?.todos;
    if (!Array.isArray(todos)) return null;
    return todos.filter((item) => item && typeof item === 'object');
  }
  return null;
}

/**
 * True when a turn produced nothing renderable yet — the state between
 * "request accepted" and the first block. The streaming card still draws
 * (the user needs to see that something is happening) but with a waiting
 * indicator instead of an empty body.
 */
export function isEmptyTurn(turn) {
  if (!turn) return true;
  return turn.blocks.length === 0 && turn.pending.size === 0;
}

// ---------------------------------------------------------------
// Internals
// ---------------------------------------------------------------

function appendBlock(turn, record) {
  turn.blocks.push(record);
  turn.index.set(record.block_id, record);
  return record;
}

function normalizeAgentId(value) {
  return typeof value === 'string' && value ? value : null;
}

/**
 * `TodoWrite` under either its plain name or an MCP-qualified one, so a
 * server that re-exports the tool still collapses to one live plan.
 */
export function isTodoWrite(name) {
  if (typeof name !== 'string' || !name) return false;
  return name === 'TodoWrite' || name.endsWith('__TodoWrite');
}
