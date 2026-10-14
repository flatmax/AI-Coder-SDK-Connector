// Peer conversations — the browser half of CC-23.
//
// Governing spec: specs5/4-features/peer-conversation.md § Browser; wire
// shapes and the header line in specs-reference/4-features/peer-conversation.md.
//
// One record, `panel._peer`, drives everything here. It arrives in two ways:
// the `peer` field of `get_current_state`, so a browser that loads or reloads
// mid-conversation sees the same thing as one that watched it start, and the
// session-wide `peerChanged` push. Null means no conversation and nothing to
// draw.
//
// What this module draws, by role and status:
//
//   responder, pending — the consent card: who is asking, the first message
//                        in full, Allow and Deny
//   responder, open    — "Peer conversation with <caller> — End", and the
//                        composer is locked (`peerLocksComposer`)
//   caller, live       — "Talking to <peer> — End", with what it waits for
//   either, ended      — who ended it, a link back to the session it
//                        replaced (responder only), and a dismiss
//
// The *from <peer>* label on a message is the one thing here that is not
// read from `_peer`. It is parsed from the message's own first line, because
// the transcript is the record: a reloaded browser, and the history browser a
// month later, must label it the way the live view did.

import { html, nothing } from 'lit';

import { renderMarkdown } from '../markdown.js';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';

/**
 * The header's shape. Must match `peer.HEADER_RE` on the server: the name is
 * the shortest run up to " (", and the root is everything up to the closing
 * ")]", so a root that itself contains parentheses still parses.
 */
const HEADER_RE = /^\[aic-dc peer message from (.+?) \((.+)\)\]$/;

/**
 * The peer that sent a user message, or null for one the user typed.
 *
 * @param {string} content
 * @returns {{repoName: string, repoRoot: string} | null}
 */
export function peerHeaderOf(content) {
  if (typeof content !== 'string') return null;
  const newline = content.indexOf('\n');
  const first = newline === -1 ? content : content.slice(0, newline);
  const match = HEADER_RE.exec(first);
  if (!match) return null;
  return { repoName: match[1], repoRoot: match[2] };
}

/** The message without its header line, for rendering under the label. */
export function peerBodyOf(content) {
  if (!peerHeaderOf(content)) return content;
  const newline = content.indexOf('\n');
  return newline === -1 ? '' : content.slice(newline + 1).replace(/^\n+/, '');
}

/** Normalise a `peerChanged` payload or snapshot field to a record or null. */
function recordOf(value) {
  if (!value || typeof value !== 'object') return null;
  if (typeof value.role !== 'string' || typeof value.status !== 'string') return null;
  return value;
}

/** Apply the `peer` field of a `get_current_state` snapshot. */
export function applyPeerSnapshot(panel, state) {
  if (!state || typeof state !== 'object' || !('peer' in state)) return;
  panel._peer = recordOf(state.peer);
}

/** The `peer-changed` window event, re-dispatched by the shell. */
export function onPeerChanged(panel, event) {
  panel._peer = recordOf(event?.detail?.state);
}

function peerName(record) {
  return record?.peer?.repo_name || 'the other repository';
}

/**
 * Whether the composer is locked: this session is answering a peer.
 *
 * Mirrors the server's lock, which is what actually refuses — this only saves
 * the user typing a message that would come back as `peer_conversation`.
 */
export function peerLocksComposer(panel) {
  const record = panel._peer;
  return !!record && record.role === 'responder' && record.status === 'open';
}

/** The placeholder the locked composer shows instead of the usual one. */
export function peerComposerPlaceholder(panel) {
  return `Peer conversation with ${peerName(panel._peer)} — End it to type here.`;
}

/**
 * Call one RPC and toast a refusal. Takes the full method name, spelled out
 * at each call site, so `tests/test_rpc_surface.py` can find the caller.
 */
async function call(panel, method, ...args) {
  try {
    const result = await panel.rpcExtract(method, ...args);
    if (result && typeof result === 'object' && result.error) {
      panel._emitToast(String(result.error), 'warning');
      return null;
    }
    return result;
  } catch (err) {
    panel._emitToast(err?.message || `Could not reach ${method}`, 'warning');
    return null;
  }
}

/** Allow or Deny on the consent card. */
export function answerPeer(panel, allow) {
  return call(panel, 'ClaudeCodeService.peer_answer', !!allow);
}

/** End, from either banner. */
export function endPeer(panel) {
  return call(panel, 'ClaudeCodeService.peer_end');
}

/** Close the ended banner. */
export function dismissPeer(panel) {
  return call(panel, 'ClaudeCodeService.peer_dismiss');
}

/**
 * Go back to the session the conversation replaced.
 *
 * Through `resume_session`, the same call the history browser makes; its
 * `sessionChanged` broadcast is what replaces the transcript, here and in
 * every other window. Dismisses the banner only once the resume is accepted.
 */
export async function returnToPreviousSession(panel) {
  const sessionId = panel._peer?.previous_session_id;
  if (!sessionId) return;
  const result = await call(panel, 'ClaudeCodeService.resume_session', sessionId, false);
  if (result !== null) await dismissPeer(panel);
}

/**
 * Who ended it, for the sentence. The two roles store this differently: the
 * responder keeps the side (`caller` / `responder`), and the caller keeps the
 * prose its agent was told ("this side", "repo2's user", "an error"), so the
 * banner and the tool result say the same thing.
 */
function whoEnded(record) {
  const by = record.ended_by ? String(record.ended_by) : '';
  if (record.role === 'responder') {
    if (by === 'responder') return 'you';
    if (by === 'caller') return peerName(record);
  }
  if (by === 'this side') return 'you';
  return by || 'one side';
}

function renderConsent(panel, record) {
  const name = peerName(record);
  return html`
    <div class="peer-banner peer-consent" role="alertdialog"
      aria-label="Peer conversation request">
      <div class="peer-lines">
        <div class="peer-line">
          <span class="peer-label">${name}</span> wants to ask this
          repository's agent about this repository.
          <span class="peer-root">${record.peer?.repo_root || ''}</span>
        </div>
        <div class="peer-line">
          Allowing starts a new session, read-only for the whole
          conversation, and you cannot type in it until the conversation
          ends. You can watch it and end it at any time.
        </div>
        ${record.first_message
          ? html`<div class="peer-first-message md-content">
              ${unsafeHTML(renderMarkdown(record.first_message))}
            </div>`
          : nothing}
      </div>
      <div class="peer-actions">
        <button class="peer-allow" @click=${() => answerPeer(panel, true)}>
          Allow
        </button>
        <button class="peer-deny" @click=${() => answerPeer(panel, false)}>
          Deny
        </button>
      </div>
    </div>
  `;
}

function renderLive(panel, record) {
  const name = peerName(record);
  let text;
  if (record.role === 'responder') {
    text = html`Peer conversation with <strong>${name}</strong>. This session
      is read-only and answering it.`;
  } else if (record.status === 'opening') {
    text = html`Talking to <strong>${name}</strong> — waiting for its user to
      allow the conversation.`;
  } else if (record.status === 'waiting') {
    text = html`Talking to <strong>${name}</strong> — waiting for its reply.`;
  } else {
    text = html`Talking to <strong>${name}</strong>.`;
  }
  return html`
    <div class="peer-banner" role="status">
      <div class="peer-lines"><div class="peer-line">${text}</div></div>
      <div class="peer-actions">
        <button class="peer-end" @click=${() => endPeer(panel)}>End</button>
      </div>
    </div>
  `;
}

function renderEnded(panel, record) {
  const name = peerName(record);
  const back = record.role === 'responder' && record.previous_session_id;
  return html`
    <div class="peer-banner peer-ended" role="status">
      <div class="peer-lines">
        <div class="peer-line">
          The peer conversation with <strong>${name}</strong> was ended by
          ${whoEnded(record)}.
          ${record.role === 'responder'
            ? 'This session stays current and remembers it.'
            : nothing}
        </div>
      </div>
      <div class="peer-actions">
        ${back
          ? html`<button class="peer-previous"
              @click=${() => returnToPreviousSession(panel)}
              title="Resume the session this conversation replaced">
              Back to previous session
            </button>`
          : nothing}
        <button class="peer-dismiss" @click=${() => dismissPeer(panel)}
          aria-label="Dismiss">✕</button>
      </div>
    </div>
  `;
}

/** The one strip this module draws, above the composer. */
export function renderPeerBanner(panel) {
  const record = panel._peer;
  if (!record) return nothing;
  if (record.status === 'ended') return renderEnded(panel, record);
  if (record.role === 'responder' && record.status === 'pending') {
    return renderConsent(panel, record);
  }
  return renderLive(panel, record);
}
