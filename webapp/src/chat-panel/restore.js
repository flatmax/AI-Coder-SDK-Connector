// Turning a transcript read off disk into messages the renderer draws.
//
// Three readers land here: `session-changed` (the user resumed a session),
// `state-loaded` (the browser reconnected to one already open), and a
// historical subagent tab (`get_subagent_transcript`). All three are the same
// backend renderer's output — `history.render_messages` — so all three
// normalize through the same function, because a turn that renders as tool
// cards when resumed and as prose when opened from a tab strip is a bug the
// user reports as "the archive lost everything".
//
// Its own module rather than a function in `events.js` so `tabs.js` can import
// it: `events.js` already imports `tabs.js`, and the cycle that would close is
// the one `rehydrateLiveAgents` works around with a dynamic import.
//
// Governing spec: specs5/5-webapp/chat.md § Engine Event Routing;
// specs5/5-webapp/subagent-browser.md § Historical Transcripts.

import { normalizeMessageContent } from '../image-utils.js';

/**
 * One restored message, in the shape the renderer reads.
 *
 * Both session restore paths — `session-changed` and `state-loaded` — go
 * through here, because a resumed session and a reconnect-restored one are
 * the same transcript arriving by two routes and must not render
 * differently. A subagent transcript joins them for the same reason.
 *
 * The old version of this kept `{role, content, images, system_event}`
 * and dropped the rest, which was harmless while the native engine's
 * records held nothing else. `history_load` renders a *turn*: an ordered
 * `blocks` list (text, thinking, tool cards, the plan), the files it
 * touched, and a footer summary of usage and duration. Flattening that
 * to prose is not a small loss of polish — it is a resumed session that
 * shows none of the work the agent did, in a UI whose whole argument for
 * tool cards is that the user should be able to see it.
 *
 * Fields are copied only when present. Absent is not the same as empty
 * here: a turn with no `turn` footer is one the transcript could not
 * supply usage for, and a fabricated empty footer would report zeros as
 * if they were measurements.
 */
export function restoreMessage(m) {
  // Multimodal messages (images) arrive as an array of
  // `{type: 'text'/'image_url', ...}` blocks; normalize to
  // `{content: <string>, images: [<data uri>]}`.
  const normalized = normalizeMessageContent(m);
  const images = Array.isArray(m.images) ? m.images : normalized.images;
  const out = { role: m.role, content: normalized.content };
  if (images.length > 0) out.images = images;
  // Image pointers, not bytes. A prompt's screenshots come back from
  // `history_load` as `{session_id, entry_uuid, block, media_type}` and are
  // resolved one at a time afterwards; carrying them is what lets the tiles
  // appear at all, and dropping them is why a resumed prompt used to lose
  // every screenshot in it.
  const refs = Array.isArray(m.image_refs)
    ? m.image_refs.filter((ref) => ref && typeof ref === 'object')
    : [];
  if (refs.length > 0) out.image_refs = refs;
  // A compact summary is a user entry because that is how the model
  // receives it, but the user did not write it — the CLI did, about the
  // context it dropped. Marked as a system event so it is labelled
  // "System" rather than attributed to the person reading it, and so
  // `seedIntoHistory` keeps it out of up-arrow recall.
  if (m.system_event || m.compact_summary) out.system_event = true;
  if (m.compact_summary) out.compact_summary = true;
  if (typeof m.turn_id === 'string' && m.turn_id) out.turn_id = m.turn_id;
  // The turn shape. `blocks` is what makes `renderMessage` treat this as
  // a Claude Code turn at all, and the rest hangs off that decision.
  if (Array.isArray(m.blocks)) out.blocks = m.blocks;
  // Rows for the subagents the turn spawned, which is also what the
  // "View subagents" affordance counts. A turn read back off disk used to
  // carry none, on the belief that the transcript does not attribute a
  // subagent to the turn that spawned it. It does: the spawn call is a tool
  // block in the turn, carrying the description and the agent type, and its
  // result names the `agentId`. The engine rebuilds rows from that
  // (`_note_subagent` in src/aic_dc/claude_code/history.py), so a refreshed
  // page keeps both the row and the way into the transcript. What a restored
  // row lacks is the live-only half — status, usage, the closing summary.
  if (Array.isArray(m.subagents)) out.subagents = m.subagents;
  if (Array.isArray(m.files) && m.files.length > 0) out.files = m.files;
  if (m.turn && typeof m.turn === 'object') out.turn = m.turn;
  // Null from a browsed turn, and null draws no badge: the transcript
  // holds no result entry, and a "completed" badge on no evidence is
  // worse than none. Only a real reason is carried through.
  if (typeof m.terminalReason === 'string' && m.terminalReason) {
    out.terminalReason = m.terminalReason;
  }
  // The mark that says the agent's memory of everything above it is now
  // a summary. Same shape the live `compact_boundary` broadcast appends,
  // so a divider read back from disk and one seen as it happened render
  // identically.
  if (m.compaction && typeof m.compaction === 'object') {
    out.compaction = m.compaction;
  }
  if (Array.isArray(m.edit_results)) out.editResults = m.edit_results;
  return out;
}
