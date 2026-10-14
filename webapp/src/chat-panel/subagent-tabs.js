// Live subagent tabs: one strip button per running `Task`, and its feed.
//
// Governing spec: specs5/5-webapp/subagent-browser.md.
//
// Until now a subagent only ever appeared as an indented row inside Main's
// turn, because `onSubagentEvent` folded it into Main's `turnBlocks.subagents`
// and its blocks nested under the `Task` card that spawned it. That is still
// true — the row is the evidence the delegation happened, and it stays — but a
// subagent doing minutes of work in a turn the user is also reading is a
// second feed competing for one message list. So it also gets a tab.
//
// Three ideas carry the whole module:
//
//   - **One id joins everything.** A block produced inside a subagent carries
//     `agent_id = the parent Task call's tool_use_id`, and a `subagentEvent`
//     carries that same id as `tool_use_id`. So `row.tool_use_id` is what
//     picks the blocks to mirror, with no correlation table (the same identity
//     `groupBlocksByScope` relies on).
//
//   - **Mirrored, not copied.** The tab's block list holds *the same record
//     objects* Main's does. `blocks.js` mutates records in place, so a tool
//     result landing on a card updates both placements at once — one card
//     object, two positions, no reconciliation. Hence the spec's "renders in
//     both the row in Main and the subagent tab from one card object".
//
//   - **Read-only, always.** There is no channel to a subagent, live or
//     finished (the SDK offers none), so the tab carries `readOnly: true` and
//     the render path drops the input surface for the one-line note instead.
//     ⏹ Stop is the only write affordance, and it ends the subagent rather
//     than talking to it.
//
// What this module deliberately does NOT do: set `currentRequestId` on a
// subagent tab. `findTabForRequest` scans that field, and a second tab
// claiming the parent's request id would steal Main's chunks. The parent id
// lives in `tab.subagent.requestId`, which is read for settling and nothing
// else.

import { makeTabState } from './state.js';
import { taskUsage } from '../turn-cost.js';
import { isConsultation } from './blocks.js';

/**
 * Terminal statuses, mapped to what a status LED is allowed to claim.
 *
 * Not derived from the SDK's `TERMINAL_TASK_STATUSES` — that set says *which*
 * statuses end a task, not what each one means, and the browser has no
 * business duplicating the vocabulary anyway (the engine sends `terminal` on
 * every event for exactly that reason). What this table adds is the verdict,
 * and its default matters more than its entries: an unrecognised terminal
 * status lands on amber, because a green dot is a claim of success and the CLI
 * will grow statuses this file has never heard of.
 */
const _TERMINAL_LED = {
  completed: 'green',
  failed: 'red',
  stopped: 'amber',
  killed: 'amber',
};

// ---------------------------------------------------------------
// Identity
// ---------------------------------------------------------------

/**
 * The strip key for one subagent.
 *
 * The SDK `agent_id` verbatim when there is one — the spec's invariant, and
 * what makes `historical:` a necessary prefix on the archived-transcript tabs
 * rather than decoration. `agent_id` is reported inside the message payload
 * rather than on the dataclass, though, so an event can arrive without it; the
 * task id then keys the tab, and a later `agent_id` is recorded on the tab's
 * own state instead of rekeying the Map. Rekeying would reorder the strip
 * under the user's cursor and orphan `_activeTabId`.
 */
export function subagentTabId(row) {
  const id = row?.agent_id || row?.task_id || row?.key;
  if (typeof id !== 'string' || !id) return null;
  // A consultation's tab is a different thing from a delegated subagent's,
  // and the prefix is what lets the rest of the strip say so in one test.
  // It is one id for the life of the consultation — the alternative, keying
  // a running one and a finished one differently, meant the tab you got
  // depended on whether you clicked before or after an event you cannot see.
  return isConsultation(row) ? `${CONSULTATION_TAB_PREFIX}${id}` : id;
}

/** Tabs opened from a consultation's card, as against a delegation's. */
export const CONSULTATION_TAB_PREFIX = 'consultation:';

export function isConsultationTab(tabId) {
  return typeof tabId === 'string'
    && tabId.startsWith(CONSULTATION_TAB_PREFIX);
}

/**
 * The subagent tab matching an id — its row key, its `agent_id`, its task id
 * or the id of the tool call that spawned it, whichever the caller has in
 * hand.
 *
 * Four fields rather than one because the four arrive at different times and
 * every caller knows a different one: the strip knows the tab id, an event
 * knows the row key, and "is this subagent already open?" is asked with an
 * `agent_id` read off a settled row.
 *
 * `tool_use_id` is the fourth and the newest. A block or a scoped notice
 * produced *inside* a subagent is stamped with the spawning call's id, not
 * with the subagent's own — that is what nests it under the right card — so
 * a handler routing one of those knows only this field. Without it a
 * consultation's "no tools, no repository access" notice fell back to the
 * main transcript, which is the one place it is not about.
 */
export function findSubagentTab(panel, id) {
  if (typeof id !== 'string' || !id) return null;
  for (const [tabId, tab] of panel._tabs) {
    const sub = tab.subagent;
    if (!sub) continue;
    if (
      sub.rowKey === id
      || sub.agent_id === id
      || sub.task_id === id
      || sub.tool_use_id === id
    ) {
      return { tabId, tab };
    }
  }
  return null;
}

/** Every live-subagent tab in strip order. */
export function subagentTabs(panel) {
  const out = [];
  for (const [tabId, tab] of panel._tabs) {
    if (tab.subagent) out.push({ tabId, tab });
  }
  return out;
}

// ---------------------------------------------------------------
// Labels
// ---------------------------------------------------------------

/** How much of a keyword fits before the strip stops being scannable. */
const _KEYWORD_MAX_LENGTH = 14;

/**
 * Tail words too generic to identify a task on their own.
 *
 * "Count webapp test files" ends in `files`, which distinguishes nothing from
 * the next task that also ends in `files`, so the keyword reaches one word
 * further back and reads `test-files`. Two words is the limit: past that the
 * label is a description again, which is what this replaced.
 */
const _GENERIC_TAIL_WORDS = new Set([
  'all', 'code', 'data', 'dir', 'directory', 'doc', 'docs', 'file', 'files',
  'folder', 'it', 'js', 'json', 'list', 'lists', 'md', 'output', 'outputs',
  'py', 'repo', 'result', 'results', 'test', 'tests', 'text', 'them', 'this',
  'that', 'thing', 'things',
]);

/**
 * Words the reach-back walks past.
 *
 * `test-files` is worth two of the label's characters; `the-files` is not. So
 * the second word is the nearest one that carries meaning, not the nearest one.
 */
const _STOPWORDS = new Set([
  'a', 'all', 'an', 'and', 'any', 'at', 'by', 'each', 'every', 'for', 'from',
  'in', 'into', 'its', 'my', 'of', 'on', 'or', 'our', 'over', 'the', 'their',
  'then', 'to', 'with',
]);

/**
 * One word that identifies a task, from the sentence describing it.
 *
 * The strip is horizontal and a tab is the only place a user chooses between
 * feeds, so a label that needs 40 characters costs the *other* tabs their
 * visibility — four tabs filled the strip before this, and a turn that fans out
 * eight left half of them behind an overflow menu. So the label is an ordinal
 * plus a keyword, and the full sentence moves to the tooltip.
 *
 * The rule is a heuristic and is meant to be read as one: English task
 * descriptions put their object last ("Count spec headings" → `headings`), so
 * the keyword is the last word, reaching back to the nearest word that isn't a
 * stopword when that last word is in `_GENERIC_TAIL_WORDS` ("check the tests" →
 * `check-tests`, not `the-tests`). Paths collapse to their basename, because
 * the live fallback description is full of absolute ones and a basename is the
 * only part of a path that identifies anything. Lowercased: these read as tags.
 *
 * Returns '' for text with no word in it, and the caller falls back to the id.
 */
export function subagentKeyword(text) {
  const raw = typeof text === 'string' ? text : '';
  const words = raw
    // /home/…/delivery.md → delivery.md, before punctuation is stripped.
    .replace(/\S*\/(\S+)/g, '$1')
    .replace(/[^A-Za-z0-9._-]+/g, ' ')
    .split(/\s+/)
    .map((word) => word.replace(/^[-._]+|[-._]+$/g, '').toLowerCase())
    .filter((word) => /[a-z0-9]/.test(word));
  if (words.length === 0) return '';

  const parts = [words[words.length - 1]];
  if (_GENERIC_TAIL_WORDS.has(parts[0])) {
    for (let i = words.length - 2; i >= 0; i -= 1) {
      if (_STOPWORDS.has(words[i])) continue;
      parts.unshift(words[i]);
      break;
    }
  }
  const keyword = parts.join('-');
  return keyword.length > _KEYWORD_MAX_LENGTH
    ? `${keyword.slice(0, _KEYWORD_MAX_LENGTH - 1)}…`
    : keyword;
}

/**
 * The label fields off the `Task` call that spawned this subagent.
 *
 * The better source, and the reason this reaches into the owner's blocks at
 * all: a live event's `description` is the SDK's *activity* string — "Running
 * List chat-panel files by modification time", rewritten on every event, so a
 * settled tab ends up labelled with whatever the subagent happened to be doing
 * when its last event landed. The `Task` card's `input.description` is what was
 * asked for, it never changes, and `input.subagent_type` is the type the user
 * chose rather than the CLI's `local_agent` for everything.
 *
 * Found by `tool_use_id`, which is a tool block's `block_id` — the same single
 * identity the block mirroring relies on. Returns null when the card is not in
 * this tab's blocks (a reconnect that replayed a subset, an event that arrived
 * before its own card), and the caller falls back to the event.
 */
function taskCardLabel(ownerTab, row) {
  const id = typeof row?.tool_use_id === 'string' ? row.tool_use_id : '';
  if (!id) return null;
  const input = ownerTab?.turnBlocks?.index?.get(id)?.tool?.input;
  if (!input || typeof input !== 'object') return null;
  const description =
    typeof input.description === 'string' ? input.description.trim() : '';
  const type =
    typeof input.subagent_type === 'string' ? input.subagent_type.trim() : '';
  if (!description && !type) return null;
  return { description, type };
}

/**
 * Resolve a tab's label fields, once, and never downgrade them.
 *
 * `labelFromTask` is the latch: the `Task` card is normally in the blocks
 * before the first event about the task it spawned, but a reconnect can replay
 * them in the other order, so an event-sourced label is allowed to be upgraded
 * to the card's exactly once and nothing may overwrite it after that. Without
 * the latch the tab would follow the activity string again, which is the bug
 * this replaced.
 */
function resolveLabel(sub, row, ownerTab) {
  if (sub.labelFromTask) return;
  const card = taskCardLabel(ownerTab, row);
  if (card) {
    sub.labelFromTask = true;
    sub.labelDescription = card.description || sub.labelDescription || '';
    sub.labelType = card.type || '';
    return;
  }
  // No card yet: keep the first description an event gave us rather than the
  // newest, for the same reason the latch exists.
  if (!sub.labelDescription && row?.description) {
    sub.labelDescription = row.description;
  }
  // `subagent_type` and not `task_type`, so the fallback names the same thing
  // the card above does — the agent's kind, not the "local_agent" transport.
  if (!sub.labelType && row?.subagent_type) sub.labelType = row.subagent_type;
}

/** The whole sentence: a tooltip, the feed's opening line, an LED's subject. */
function describe(sub) {
  const type = sub?.labelType || sub?.subagent_type || sub?.agent_type || '';
  const desc = sub?.labelDescription || sub?.description || '';
  if (type && desc) return `${type} — ${desc}`;
  return desc || type || sub?.agent_id || sub?.task_id || 'Subagent';
}

/**
 * The strip label: an ordinal and a keyword — `1 headings`.
 *
 * The ordinal is assigned at creation and never recomputed, so a tab keeps its
 * number for the life of the turn even as others settle around it; it is what
 * makes two subagents asked to do similar things still distinguishable, and
 * what a user says out loud ("the second one").
 */
function subagentTabLabel(sub) {
  const keyword = subagentKeyword(describe(sub));
  const ordinal = sub?.ordinal;
  if (!keyword) return ordinal ? `Subagent ${ordinal}` : 'Subagent';
  return ordinal ? `${ordinal} ${keyword}` : keyword;
}

/**
 * The strip tooltip for one subagent tab: the full sentence the label dropped.
 *
 * Exported because `tabs.js` builds every other tab's tooltip from its label,
 * and this is the one tab whose label is deliberately not the whole story.
 */
export function subagentTabTooltip(tab) {
  const sub = tab?.subagent;
  if (!sub) return '';
  const ordinal = sub.ordinal ? `${sub.ordinal} · ` : '';
  return `${ordinal}${describe(sub)} — subagent feed (read-only)`;
}

// ---------------------------------------------------------------
// Tab lifecycle
// ---------------------------------------------------------------

/**
 * Create or update the tab for one subagent row.
 *
 * Called for every `subagentEvent`, not just `started`: creating on first
 * sight of a task survives a missed message, and a `started` we never saw
 * would otherwise mean a subagent that runs to completion with no tab at all.
 * If that first event is already terminal the tab is created and settled in
 * the same call — a subagent that finished before the browser heard of it
 * still leaves its feed behind, which is the whole point of keeping the tab
 * for the rest of the turn.
 *
 * Returns `{tabId, tab, created}`, or null for a row with no usable id or one
 * whose id collides with a tab that is not a subagent's (never clobber Main,
 * an agent tab, or an archived transcript) — or, for a consultation, one
 * nobody has asked to see (see `options.onDemand`).
 *
 * A consultation's default surface is its inline card, so creating a tab for
 * every one of them filled the strip with feeds competing for attention with
 * the stream the reader was already in. This function reconciles engine state
 * into tabs and so never creates one for a consultation; that is
 * `openConsultationTab`'s job, and the user's click is what calls it.
 * **Updating** an existing tab is never gated: once a tab has been asked for
 * it tracks every later event like any other, from this function, through
 * exactly the path a delegated subagent's does.
 */
export function syncSubagentTab(panel, requestId, row, ownerTab) {
  return _syncSubagentTab(
    panel, requestId, row, ownerTab, !isConsultation(row),
  );
}

function _syncSubagentTab(panel, requestId, row, ownerTab, allowCreate) {
  if (!row || typeof row !== 'object') return null;
  const key = typeof row.key === 'string' && row.key ? row.key : null;
  if (!key) return null;

  let found = findSubagentTab(panel, key)
    || (row.agent_id ? findSubagentTab(panel, row.agent_id) : null);
  let created = false;
  let ordinal = 0;

  if (!found) {
    if (!allowCreate) return null;
    const tabId = subagentTabId(row);
    if (!tabId || tabId === 'main') return null;
    if (panel._tabs.has(tabId)) return null;
    // Counted before the tab is inserted, so the first subagent of a turn is
    // 1. Consultations are left out of the numbering on both sides: they are
    // not workers in a fan-out, and one left open across a send (see
    // `clearSubagentTabs`) would otherwise make the next turn's first
    // delegation `2` with no `1` beside it. Numbering them by turn instead
    // is worse, not better — it puts two tabs labelled `1` in the strip.
    // One past the highest rather than a count: an older turn's tabs survive
    // a send while its subagents run (`keepRequests`), and a count taken after
    // some of them left would hand out a number still on screen.
    ordinal = isConsultation(row)
      ? 0
      : subagentTabs(panel)
        .filter(({ tab }) => !isConsultation(tab.subagent))
        .reduce((most, { tab }) => Math.max(most, tab.subagent.ordinal || 0), 0)
        + 1;
    const state = makeTabState();
    // No input surface at all on this tab — see the module note.
    state.readOnly = true;
    state.streaming = !row.terminal;
    // The run timer starts now rather than at the subagent's real start: the
    // engine reports no start time for a task, and a counter that began when
    // we first heard of it is the honest reading of "how long have I been
    // watching this".
    state.streamStartedAt = row.terminal ? null : Date.now();
    panel._tabs.set(tabId, state);
    found = { tabId, tab: state };
    created = true;
  }

  const tab = found.tab;
  const previous = tab.subagent;
  // The row object is replaced wholesale on every event by
  // `applySubagentEvent` (it patches into a fresh object), so holding a
  // reference would freeze the tab at the first event. Copy the fields, and
  // copy them from the row rather than merging the payload — the row is
  // already the accumulation of every event for this task.
  tab.subagent = {
    ...row,
    rowKey: key,
    // The *parent* turn, for settling. Deliberately not `currentRequestId`.
    requestId: previous?.requestId || requestId || null,
    settled: previous?.settled || false,
    unknown: previous?.unknown || false,
    errored: previous?.errored || false,
    feedMessage: previous?.feedMessage || false,
    // Carried for the same reason as `feedMessage`: the transcript fallback
    // (`loadSubagentFeedIfEmpty` in tabs.js) latches here, and a trailing
    // `notification` arriving after an `updated` already settled the tab would
    // otherwise reset the latch and read the transcript a second time into the
    // same feed.
    transcriptRead: previous?.transcriptRead || false,
    // Label state, carried across the wholesale replace above: the strip's
    // number and the sentence behind its keyword, both resolved once.
    ordinal: previous?.ordinal || ordinal,
    labelDescription: previous?.labelDescription || '',
    labelType: previous?.labelType || '',
    labelFromTask: previous?.labelFromTask || false,
  };
  resolveLabel(tab.subagent, row, ownerTab);
  panel._tabLabels.set(found.tabId, subagentTabLabel(tab.subagent));
  seedDescription(tab);

  if (row.terminal && !tab.subagent.settled) {
    settleSubagentTab(panel, tab);
  }
  return { ...found, created };
}

/**
 * The opening line of a subagent's feed: what it was asked to do.
 *
 * Per the spec's empty state — a subagent whose transcript has not started
 * "shows the description and a working indicator, not an empty message list".
 * Rewritten in place when a later event supplies a description the first one
 * lacked, so the tab does not keep an opaque id as its only content.
 *
 * Shaped as a system event, like a commit notice or the unreadable-transcript
 * marker: this is AIC⚡DC describing the subagent, not anything the subagent
 * said.
 */
function seedDescription(tab) {
  const line = `🤖 ${describe(tab.subagent)}`;
  const first = tab.messages[0];
  if (first && first.subagent_seed) {
    if (first.content === line) return;
    tab.messages = [
      { ...first, content: line },
      ...tab.messages.slice(1),
    ];
    return;
  }
  tab.messages = [
    {
      role: 'user',
      content: line,
      system_event: true,
      subagent_seed: true,
    },
    ...tab.messages,
  ];
}

/**
 * Attach the feed's block list to a message, once.
 *
 * The message *shares* the live array rather than taking a frozen copy, which
 * is the opposite of what Main does at `streamComplete` and for the opposite
 * reason. Main freezes so a late event cannot rewrite history the user has
 * read; a subagent tab has no next turn to protect against — its blocks are
 * mirrored from Main and stop arriving when Main's turn ends — and a tool
 * result that lands after the terminal status is content the user still wants.
 */
function ensureFeedMessage(tab) {
  const sub = tab.subagent;
  if (!sub || sub.feedMessage) return false;
  sub.feedMessage = true;
  tab.messages = [
    ...tab.messages,
    { role: 'assistant', content: '', blocks: tab.turnBlocks.blocks },
  ];
  return true;
}

/**
 * Settle one subagent tab: the feed stops, the tab stays.
 *
 * "Archived transcript in place" — the tab keeps its position in the strip for
 * the rest of the turn, because a tab that vanished the moment its subagent
 * finished would take the record with it just as the user went to read it.
 *
 * `unknown` is the turn-end case: a subagent still live when the parent turn
 * ends has no reported outcome, and the spec is explicit that it must never be
 * shown as completed. `errored` is the same case on a turn that failed, where
 * red is the more honest colour than amber.
 */
export function settleSubagentTab(panel, tab, options = {}) {
  const sub = tab?.subagent;
  if (!sub || sub.settled) return false;
  sub.settled = true;
  if (options.unknown) sub.unknown = true;
  if (options.errored) sub.errored = true;
  tab.streaming = false;
  tab.streamStartedAt = null;
  if (tab.turnBlocks.blocks.length > 0) ensureFeedMessage(tab);
  return true;
}

/**
 * Settle every subagent tab still live on a parent request.
 *
 * Called from `streamComplete`. A subagent still marked live here is one whose
 * terminal event never arrived, not one still working, so its status is
 * *unknown* — a distinct thing to say and the reason the amber LED exists.
 *
 * That reading is only true of the tasks this result actually ended, which is
 * why `stillRunning` exists. **A result message ends a turn, not the run**: a
 * background subagent outlives the turn that spawned it, the engine keeps
 * following it (`session.py` § `_complete`), and it names what is still
 * going in `background_tasks`. Settling those was this bug's most visible face
 * — an amber "status unknown at turn end" LED on a subagent that went on to
 * finish successfully seconds later, on a tab whose feed was empty because
 * nothing after the result had been read.
 *
 * Tabs whose `requestId` names a different turn are left alone: an older
 * turn's tabs stay in the strip while its background subagents run
 * (`clearSubagentTabs` § `keepRequests`), and this turn's result says nothing
 * about them.
 */
export function settleLiveSubagentTabs(panel, requestId, errored, stillRunning = []) {
  const running = new Set(Array.isArray(stillRunning) ? stillRunning : []);
  let changed = false;
  for (const { tab } of subagentTabs(panel)) {
    const sub = tab.subagent;
    if (sub.settled) continue;
    if (requestId && sub.requestId && sub.requestId !== requestId) continue;
    if (sub.task_id && running.has(sub.task_id)) continue;
    const unknown = !sub.terminal;
    if (settleSubagentTab(panel, tab, {
      unknown,
      errored: unknown && !!errored,
    })) {
      changed = true;
    }
  }
  return changed;
}

/**
 * Drop every subagent tab from the strip.
 *
 * Per § Tab Lifetime: a new turn starts from Main alone, and so does a new or
 * resumed session. The subagents of the previous turn are finished and their
 * transcripts are on disk — reachable through "View subagents" on the settled
 * turn, which is where a record of past work belongs. Keeping the tabs would
 * accumulate a strip the user has to clean up by hand.
 *
 * `keepConsultations` spares the one kind that argument does not cover, and
 * the send is the only caller that passes it. A consultation tab exists
 * because the user clicked to open it, and the reason to open it is to keep
 * the second opinion beside the composer while writing the reply to it —
 * which is exactly the keystroke that would destroy it. It is not lost work
 * either way (the answer stays on its card, inline), but a reference that
 * vanishes at the moment of use is no reference. The session change still
 * takes it: those blocks belong to a transcript that is no longer on screen.
 *
 * So a consultation tab is closed by a session change and by nothing else,
 * and the strip can hold as many as the user has opened. That is bounded by
 * deliberate clicks rather than by traffic — the old eager creation, which
 * this replaces, was the unbounded one — but there is no way to close one by
 * hand, which is AG-R-31.
 *
 * `keepRequests` spares the tabs of turns the send is not ending: a turn
 * parked with its background subagents still running (`parkBackgroundTurn`
 * in streaming.js). Their feeds are still arriving, and the tab is the only
 * place they show until that turn settles. The session change passes nothing
 * and takes them too.
 *
 * Switches to Main before deleting when the active tab is one of them, so the
 * per-tab accessors are never left pointing at a missing key (they would
 * silently lazy-read `undefined[field]` and throw).
 */
export function clearSubagentTabs(
  panel,
  { keepConsultations = false, keepRequests = null } = {},
) {
  const open = subagentTabs(panel).filter(
    ({ tab }) => !(keepConsultations && isConsultation(tab.subagent))
      && !keepRequests?.has(tab.subagent.requestId),
  );
  if (open.length === 0) return false;
  if (open.some(({ tabId }) => tabId === panel._activeTabId)) {
    panel._activeTabId = 'main';
  }
  for (const { tabId } of open) {
    panel._tabs.delete(tabId);
    panel._tabLabels.delete(tabId);
  }
  return true;
}

// ---------------------------------------------------------------
// Block mirroring
// ---------------------------------------------------------------

/**
 * Mirror the owning tab's subagent blocks into their own tabs.
 *
 * Idempotent and cheap: a block already in a tab's index is skipped, and the
 * whole pass returns immediately when no subagent tab is open — which is every
 * turn that delegates nothing, i.e. most of them.
 *
 * The records are pushed by reference. That is what makes one tool card render
 * in Main's subagent row and in the subagent's tab as the same object, so a
 * result attaching to it appears in both places without a second write.
 *
 * The mirrored tab's own `subagents` map is left empty on purpose:
 * `groupBlocksByScope` emits a block whose parent has no row as a top-level
 * entry, so the feed renders flat — no row header wrapping a tab that is
 * already entirely about that one subagent.
 *
 * @returns {boolean} whether the *active* tab gained anything, i.e. whether a
 *   repaint is owed. Background tabs are updated silently and draw when the
 *   user switches to them, the same contract as `markBlocksDirty`.
 */
export function mirrorSubagentBlocks(panel, ownerTab) {
  // A subagent tab is a mirror target, never a source — mirroring from one
  // would re-mirror its own blocks (they all carry an `agent_id`) straight
  // back into itself.
  if (!ownerTab || ownerTab.subagent) return false;
  const blocks = ownerTab.turnBlocks?.blocks;
  if (!blocks || blocks.length === 0) return false;

  const byParent = new Map();
  for (const { tabId, tab } of subagentTabs(panel)) {
    const parent = tab.subagent.tool_use_id;
    if (typeof parent === 'string' && parent) {
      byParent.set(parent, { tabId, tab });
    }
  }
  if (byParent.size === 0) return false;

  let activeChanged = false;
  for (const block of blocks) {
    const parent = block?.agent_id;
    if (!parent) continue;
    const target = byParent.get(parent);
    if (!target) continue;
    const mirror = target.tab.turnBlocks;
    if (mirror.index.has(block.block_id)) continue;
    mirror.blocks.push(block);
    mirror.index.set(block.block_id, block);
    // A block arriving after the tab settled — the subagent's last tool call
    // racing its own terminal status. The settled tab has no streaming card
    // left to draw it, so it needs the feed message it never got.
    if (target.tab.subagent.settled) ensureFeedMessage(target.tab);
    if (target.tabId === panel._activeTabId) activeChanged = true;
  }
  return activeChanged;
}

/**
 * Every block the turn recorded for one subagent, from wherever it is now.
 *
 * A turn holds its blocks on `turnBlocks` while it runs and in a settled
 * message when it ends, and which one has them is a fact about the *turn*,
 * not about the subagent. Searching both is what lets the caller stop caring:
 * one consultation tab, whether the turn that asked it is still going or not.
 * A turn parked behind a newer one (`parkBackgroundTurn`) is still going.
 */
export function findSubagentBlocks(ownerTab, toolUseId) {
  if (!toolUseId || !ownerTab || ownerTab.subagent) return [];
  const running = [
    ownerTab.turnBlocks,
    ...Array.from(ownerTab.backgroundTurns?.values() ?? [], (parked) => parked.turnBlocks),
  ];
  for (const turn of running) {
    const live = turn?.blocks;
    if (!Array.isArray(live)) continue;
    const mine = live.filter((block) => block?.agent_id === toolUseId);
    if (mine.length > 0) return mine;
  }
  const messages = Array.isArray(ownerTab.messages) ? ownerTab.messages : [];
  // Backwards: the turn being asked about is nearly always a recent one, and
  // a tool use id appears on exactly one turn, so the first hit is the only
  // hit and the scan stops there.
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const blocks = messages[i]?.blocks;
    if (!Array.isArray(blocks)) continue;
    const mine = blocks.filter((block) => block?.agent_id === toolUseId);
    if (mine.length > 0) return mine;
  }
  return [];
}

/**
 * Open a consultation's tab because the user asked for one.
 *
 * The counterpart to the refusal in `syncSubagentTab`: nothing creates a
 * consultation's tab on its own, so this is the only way one comes into
 * being, and the user's click is what calls it.
 *
 * One tab, whenever it is asked for. `row.terminal` looks like a reason to
 * build something different for a finished consultation and is not: a
 * consultation is one question and one answer, so it is usually over well
 * inside the turn that asked it, and a live tab that finishes simply becomes
 * a viewer of its own blocks. Keying on it — or on whether the turn had
 * settled — meant the entity you got depended on a boundary you cannot see,
 * with two id spaces and two lifetimes behind it. What the turn's state
 * decides here is only *where the blocks are read from*, which nobody can
 * see either and nobody needs to.
 *
 * Returns the tab id, or null when the row cannot have one.
 */
export function openConsultationTab(panel, requestId, row, ownerTab) {
  if (!isConsultation(row)) return null;
  const synced = _syncSubagentTab(panel, requestId, row, ownerTab, true);
  if (!synced) return null;
  // The live route. A tab created partway through a consultation starts with
  // an empty mirror and the blocks it missed are still on the owner's turn
  // state; `mirrorSubagentBlocks` is idempotent on `block_id`, so the
  // catch-up and the next streamed block cannot double up.
  mirrorSubagentBlocks(panel, ownerTab);
  for (const parked of ownerTab?.backgroundTurns?.values() ?? []) {
    mirrorSubagentBlocks(panel, parked);
  }
  if (synced.tab.turnBlocks.blocks.length === 0) {
    // The settled route: the turn ended and emptied the list mirroring
    // reads, so its record is in a message now. Projected into the feed
    // shape `ensureFeedMessage` gives a settled subagent tab, so it draws
    // through the renderer the user already read the answer in. Blocks are
    // shared by reference, as mirroring already shares them — the settled
    // turn froze its own copies and nothing writes to them again.
    const mine = findSubagentBlocks(ownerTab, row.tool_use_id);
    if (mine.length > 0) {
      synced.tab.messages = [{ role: 'assistant', content: '', blocks: mine }];
      synced.tab.subagent.feedMessage = true;
    }
  }
  // Activated unconditionally, including when the tab already existed: the
  // click is "show me this", and a second click that silently did nothing
  // because the first one worked reads as a broken button.
  panel._activeTabId = synced.tabId;
  panel.requestUpdate?.();
  return synced.tabId;
}

// ---------------------------------------------------------------
// Reconnect
// ---------------------------------------------------------------

/**
 * Rebuild subagent tabs from an `active_streams[].subagents` snapshot.
 *
 * A browser that refreshes mid-turn has to find the same strip it left, or the
 * running subagents become invisible until the turn ends. The engine keeps the
 * rows for the turn in flight and reports them beside the block state; each
 * entry is the same shape a live `subagentEvent` carries, so it goes through
 * `syncSubagentTab` unchanged and creation stays idempotent.
 *
 * Transcripts are not fetched here. The blocks the snapshot replays into Main
 * mirror across on the pass that follows, and everything else the subagent
 * wrote is in its on-disk transcript — read only if the user opens the tab,
 * per § Refresh and Reconnect.
 */
export function rehydrateSubagentTabs(panel, requestId, subagents, ownerTab) {
  if (!Array.isArray(subagents) || subagents.length === 0) return false;
  // What the user was looking at before the strip regrew under them. A tab
  // created here is a tab nobody has chosen yet, so none of them may end up
  // active: a read-only feed as the landing tab after a refresh takes the
  // input surface away with no gesture that asked for it. Observed once on
  // 2026-08-17 and never reproduced, which is the argument for asserting the
  // property rather than for hunting the cause.
  const wasActive = panel._activeTabId;
  const opened = [];
  for (const entry of subagents) {
    if (!entry || typeof entry !== 'object') continue;
    const key = entry.key
      || entry.task_id
      || entry.agent_id
      || entry.tool_use_id;
    if (typeof key !== 'string' || !key) continue;
    const synced = syncSubagentTab(panel, requestId, { ...entry, key }, ownerTab);
    if (synced?.created) opened.push(synced.tabId);
  }
  if (opened.includes(panel._activeTabId)) {
    panel._activeTabId = panel._tabs.has(wasActive) ? wasActive : 'main';
  }
  return opened.length > 0;
}

// ---------------------------------------------------------------
// Status LEDs
// ---------------------------------------------------------------

/**
 * The LED state for one subagent tab: cyan, green, red or amber.
 *
 * Cyan while it runs, and the three resting states are the distinction the
 * spec's fourth colour was added for — `stopped` and `killed` are neither a
 * success nor a fault, and neither is a subagent whose outcome the turn never
 * reported. Rolling them into green would claim work finished that may not
 * have; rolling them into red would report a fault where the user pressed
 * Stop.
 */
export function subagentLedState(sub) {
  if (!sub) return 'cyan';
  if (!sub.terminal && !sub.settled) return 'cyan';
  if (sub.unknown || !sub.terminal) return sub.errored ? 'red' : 'amber';
  const status = typeof sub.status === 'string' ? sub.status : '';
  return _TERMINAL_LED[status] || 'amber';
}

/**
 * The LED tooltip, in the four forms § Status LEDs specifies.
 *
 * The green form's counters come from the SDK's `TaskUsage`
 * (`{total_tokens, tool_uses, duration_ms}`), which shares no field names with
 * the per-turn token counters — reading it with `turnTokens` is what made the
 * row's token chip permanently blank. Either counter is dropped when the
 * payload does not carry it rather than printed as zero: "0 tools" is a claim
 * about a subagent that ran, and an absent number is not that claim.
 */
export function subagentLedTooltip(sub, state) {
  const desc = describe(sub);
  if (state === 'cyan') {
    const tool = sub?.last_tool_name;
    return tool ? `${desc}: running — ${tool}` : `${desc}: running`;
  }
  if (state === 'green') {
    const { tokens, toolUses } = taskUsage(sub?.usage);
    const parts = [];
    if (toolUses > 0) {
      parts.push(`${toolUses} ${toolUses === 1 ? 'tool' : 'tools'}`);
    }
    if (tokens > 0) parts.push(`${tokens.toLocaleString()} tokens`);
    return parts.length > 0
      ? `${desc}: completed (${parts.join(', ')})`
      : `${desc}: completed`;
  }
  if (sub?.unknown) return `${desc}: status unknown at turn end`;
  if (state === 'red') return `${desc}: failed`;
  return `${desc}: ${sub?.status || 'stopped'}`;
}
