// HistoryBrowser — modal overlay for browsing past sessions.
//
// Conversion phase 5. Every RPC behind this modal belonged to the
// native engine and now belongs to `ClaudeCodeService`, reading the
// CLI's own transcript mirrored under `.ac-dc4/sessions/` rather than
// the native engine's `history.jsonl`. Two consequences reach the UI:
//
//   - **The reads answer a union.** `history_list`, `history_load` and
//     `history_search` each return a bare list on success and
//     `{error}` on failure. This component used to collapse both into
//     `Array.isArray(x) ? x : []`, which draws "could not read your
//     history" as "you have no history" — the two want opposite
//     reactions from the user, so they are told apart here and the
//     failure is shown where the list would have been.
//   - **Loading a session resumes an engine, not a context.**
//     `resume_session` hands the transcript to the CLI, which rebuilds
//     its own context from it; nothing is read back into a prompt.
//     That is why the footer says Resume rather than "Load into
//     context", and why Fork sits beside it: a fork cannot damage the
//     original, which makes it the safe way to revisit an old
//     conversation, and the spec asks for it wherever resume is
//     offered. A session whose transcript did not survive comes back
//     `resumable: false` and is labelled rather than failed on click.
//   - **Deleting is irreversible and takes three files with it.** The
//     transcript is the one thing under `.ac-dc4/` that does not
//     rebuild, and it holds the session's pasted images; the events
//     log and the derived index lose their rows for it too. So Delete
//     arms on the first click and acts on the second, and the row
//     leaves the list on the `sessionDeleted` broadcast rather than
//     locally — every open browser has the same stale row to drop,
//     including this one.
//   - **Images arrive as pointers, not bytes.** A prompt's image
//     blocks come back from `history_load` as `image_refs`, and each
//     one is resolved separately by `history_image`. Entries hold
//     pasted images verbatim as base64, so a load that inlined them
//     would push megabytes at every client on every open and again on
//     every reconnect.
//
// Responsibilities:
//
//   - List past sessions newest-first, with preview + message count
//     + timestamp
//   - Full-text search across all sessions, debounced to avoid
//     flooding the server while the user types
//   - Preview of a selected session's messages (simplified
//     rendering — role labels + raw content, no file-mention
//     wrapping), resolving each image pointer in it to a thumbnail
//   - Resume or fork the selected session, which triggers the
//     server's sessionChanged broadcast that the chat panel's
//     existing handler consumes
//   - Delete a session, in two clicks, dropping the row when the
//     server confirms it
//   - Keyboard shortcuts: Escape closes (or clears search
//     first if the query is non-empty and the search input is
//     focused)
//   - Backdrop click closes, close button closes
//
// Governing spec: specs5/3-engine/history.md § Resume, Fork, and New;
// specs4/5-webapp/chat.md "History Browser" for the layout.
//
// Event contract:
//   - `close` (bubbles, composed) — dispatched when the user
//     closes the modal via any path. Parent (chat panel) toggles
//     the modal open-state off.
//   - `session-loaded` (bubbles, composed) — dispatched AFTER
//     the RPC call succeeds, carrying `{session_id, action}`. The
//     server also broadcasts sessionChanged to all clients; the chat
//     panel listens for that independently. The event is fired
//     locally so the parent can distinguish "user loaded from
//     history" (close the modal) from "another client changed
//     session" (just reflect the change). The broadcast path
//     alone would close the modal on every remote session switch.
//
// The server's sessionChanged broadcast includes the full
// message list; the chat panel's _onSessionChanged handler
// replaces `messages` wholesale. Nothing in this component
// needs to mutate the chat panel directly.

import { LitElement, css, html } from 'lit';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';

import { RpcMixin } from './rpc-mixin.js';
import { withRpcTimeout } from './rpc.js';
import { renderMarkdown } from './markdown.js';
import { normalizeMessageContent } from './image-utils.js';
import {
  hydrateImageRefs,
  imageRefKey,
  imageRefsOf,
} from './image-refs.js';
import { segmentResponse, matchSegmentsToResults } from './edit-blocks.js';
import { renderEditCard } from './edit-block-render.js';

/**
 * Debounce delay for search queries. 300ms matches the
 * specs4/5-webapp/search.md file-search value and
 * is short enough to feel responsive while coalescing bursts
 * of typing into a single RPC call.
 */
const SEARCH_DEBOUNCE_MS = 300;

/**
 * Deadline for every history call. Well above the real work — a
 * listing is one batch read of the summary sidecars plus one parse per
 * session, all on the server's executor — so reaching it means no
 * reply is coming at all, which is the only case `withRpcTimeout` is
 * for (see rpc.js).
 *
 * Without it a dropped reply is a spinner that never resolves, and
 * this modal is nothing but spinners and lists: "Loading sessions…"
 * forever says the server is working when nothing is on its way. The
 * resume path is worse than cosmetic — its in-flight guard is cleared
 * in a `finally` that never runs, so the button stays disabled for the
 * rest of the session.
 */
const HISTORY_TIMEOUT_MS = 30000;

/**
 * The `{error}` half of a history read's return union, or null when
 * the call answered with data.
 *
 * `history_list`, `history_load` and `history_search` each return a
 * bare list on success and `{error: "…"}` on failure, deliberately:
 * an empty list would conflate "no history yet" with "could not read
 * it", and only the second is worth a user's attention
 * (specs5/3-engine/history.md).
 */
function historyError(result) {
  if (Array.isArray(result)) return '';
  if (result && typeof result === 'object' && result.error) {
    return String(result.error);
  }
  return '';
}

/**
 * Whether a listed session can be resumed. Absent means unknown —
 * a row from a backend that does not report the field — and only an
 * explicit `false` disables the buttons, so an unknown never costs
 * the user a session they could have opened.
 */
function isResumable(session) {
  return !session || session.resumable !== false;
}

/**
 * Format an ISO-8601 timestamp as a short relative-time string
 * for the session list. "12m ago", "3h ago", "2d ago". Falls
 * back to the raw string if parsing fails — defensive against
 * malformed data.
 */
function formatRelativeTime(iso) {
  if (!iso) return '';
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return iso;
  const deltaSec = Math.max(0, (Date.now() - then) / 1000);
  if (deltaSec < 60) return 'just now';
  const deltaMin = Math.floor(deltaSec / 60);
  if (deltaMin < 60) return `${deltaMin}m ago`;
  const deltaHr = Math.floor(deltaMin / 60);
  if (deltaHr < 24) return `${deltaHr}h ago`;
  const deltaDay = Math.floor(deltaHr / 24);
  if (deltaDay < 30) return `${deltaDay}d ago`;
  // Older than a month — show the date only.
  try {
    return new Date(then).toLocaleDateString();
  } catch (_) {
    return iso;
  }
}

export class HistoryBrowser extends RpcMixin(LitElement) {
  static properties = {
    /**
     * Whether the modal is shown. Parent toggles this; the
     * component dispatches `close` but doesn't flip its own
     * prop (parent is the source of truth).
     */
    open: { type: Boolean, reflect: true },
    /** Session summaries from history_list. */
    _sessions: { type: Array, state: true },
    /** Loading state for the session list. */
    _loadingSessions: { type: Boolean, state: true },
    /**
     * Why the session list is missing, when it is. Empty string
     * when the list simply has nothing in it — an empty history and
     * an unreadable one are different sentences.
     */
    _listError: { type: String, state: true },
    /** Currently selected session ID (left pane click). */
    _selectedSessionId: { type: String, state: true },
    /** Messages for the selected session (right pane). */
    _selectedMessages: { type: Array, state: true },
    /** Loading state for the selected session's messages. */
    _loadingMessages: { type: Boolean, state: true },
    /**
     * Why the selected session would not load. This is the same
     * condition that makes a session non-resumable, so the reason
     * shown here is also the reason Resume is disabled.
     */
    _messagesError: { type: String, state: true },
    /** Current search query (may be empty). */
    _searchQuery: { type: String, state: true },
    /** Whether search results mode is active. */
    _searchMode: { type: Boolean, state: true },
    /** Search hits from history_search. */
    _searchHits: { type: Array, state: true },
    /** Why the search failed, when it did. */
    _searchError: { type: String, state: true },
    /**
     * Which session action is in flight: `'resumed'`, `'forked'`, or
     * null. Both buttons are disabled while either runs — they
     * attach the one engine to one conversation, so a second click
     * is a race between two answers to the same question.
     */
    _loadingSession: { type: String, state: true },
    /**
     * The session id whose Delete button is armed, or null. Deleting
     * a transcript is the one thing in this modal that cannot be
     * undone — it is also the one thing under `.ac-dc4/` that does
     * not rebuild — so it takes two clicks on the same session.
     */
    _confirmDelete: { type: String, state: true },
    /** True while a delete RPC is in flight. */
    _deleting: { type: Boolean, state: true },
    /**
     * Context menu state. Null when closed; otherwise
     * `{x, y, message}` — position in viewport coordinates
     * and the message the menu targets.
     */
    _contextMenu: { type: Object, state: true },
  };

  static styles = css`
    :host {
      display: none;
    }
    :host([open]) {
      display: block;
    }
    .backdrop {
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.5);
      z-index: 100;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .modal {
      background: var(--bg-primary, #0d1117);
      border: 1px solid rgba(240, 246, 252, 0.15);
      border-radius: 8px;
      width: min(90vw, 900px);
      height: min(85vh, 700px);
      display: flex;
      flex-direction: column;
      overflow: hidden;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.6);
    }
    .modal-header {
      display: flex;
      align-items: center;
      gap: 0.75rem;
      padding: 0.75rem 1rem;
      border-bottom: 1px solid rgba(240, 246, 252, 0.1);
      background: rgba(22, 27, 34, 0.6);
    }
    .modal-title {
      font-weight: 600;
      font-size: 0.9375rem;
      flex-shrink: 0;
    }
    .search-input {
      flex: 1;
      padding: 0.35rem 0.6rem;
      background: rgba(13, 17, 23, 0.9);
      border: 1px solid rgba(240, 246, 252, 0.15);
      border-radius: 4px;
      color: var(--text-primary, #c9d1d9);
      font-family: inherit;
      font-size: 0.875rem;
    }
    .search-input:focus {
      outline: none;
      border-color: var(--accent-primary, #58a6ff);
    }
    .close-button {
      background: transparent;
      border: 1px solid transparent;
      color: var(--text-secondary, #8b949e);
      padding: 0.25rem 0.5rem;
      font-size: 1rem;
      line-height: 1;
      cursor: pointer;
      border-radius: 4px;
    }
    .close-button:hover {
      background: rgba(240, 246, 252, 0.08);
      color: var(--text-primary, #c9d1d9);
    }
    .modal-body {
      flex: 1;
      min-height: 0;
      display: flex;
    }
    .sessions-pane {
      flex: 0 0 300px;
      border-right: 1px solid rgba(240, 246, 252, 0.1);
      overflow-y: auto;
      display: flex;
      flex-direction: column;
    }
    .session-item {
      padding: 0.6rem 0.75rem;
      cursor: pointer;
      border-bottom: 1px solid rgba(240, 246, 252, 0.05);
    }
    .session-item:hover {
      background: rgba(240, 246, 252, 0.04);
    }
    .session-item.selected {
      background: rgba(88, 166, 255, 0.1);
      border-left: 3px solid var(--accent-primary, #58a6ff);
      padding-left: calc(0.75rem - 3px);
    }
    .session-preview {
      font-size: 0.8125rem;
      color: var(--text-primary, #c9d1d9);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .session-meta {
      display: flex;
      gap: 0.5rem;
      margin-top: 0.25rem;
      font-size: 0.7rem;
      color: var(--text-secondary, #8b949e);
    }
    .msg-count {
      background: rgba(240, 246, 252, 0.08);
      padding: 0.05rem 0.35rem;
      border-radius: 3px;
    }
    /* A session the engine cannot resume — deleted transcript, or
     * from before the conversion. Still browsable, so it reads as a
     * qualifier on the row rather than as a failure. */
    .not-resumable {
      background: rgba(210, 153, 34, 0.15);
      color: #d29922;
      padding: 0.05rem 0.35rem;
      border-radius: 3px;
    }
    .empty-list {
      padding: 1rem;
      color: var(--text-secondary, #8b949e);
      font-style: italic;
      text-align: center;
    }
    /* "Could not read your history", never "you have no history".
     * Coloured, because the empty state next to it is the same
     * shape and the two must not be mistaken for each other. */
    .error-note {
      padding: 1rem;
      color: #f85149;
      font-size: 0.8125rem;
      text-align: center;
    }
    .loading-note {
      padding: 1rem;
      color: var(--text-secondary, #8b949e);
      font-style: italic;
      text-align: center;
    }
    .preview-pane {
      flex: 1;
      min-width: 0;
      display: flex;
      flex-direction: column;
    }
    .preview-messages {
      flex: 1;
      overflow-y: auto;
      padding: 1rem;
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
    }
    .preview-empty {
      margin: auto;
      color: var(--text-secondary, #8b949e);
      font-style: italic;
    }
    .preview-message {
      border-radius: 6px;
      padding: 0.5rem 0.75rem;
      font-size: 0.875rem;
    }
    .preview-message.role-user {
      background: rgba(88, 166, 255, 0.06);
      border: 1px solid rgba(88, 166, 255, 0.15);
    }
    .preview-message.role-assistant {
      background: rgba(240, 246, 252, 0.03);
      border: 1px solid rgba(240, 246, 252, 0.08);
    }
    .preview-message.role-system {
      background: rgba(240, 246, 252, 0.03);
      border: 1px dashed rgba(240, 246, 252, 0.15);
      color: var(--text-secondary, #8b949e);
      font-style: italic;
    }
    .preview-role-label {
      font-size: 0.7rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      opacity: 0.55;
      margin-bottom: 0.25rem;
    }
    .preview-body :first-child {
      margin-top: 0;
    }
    .preview-body :last-child {
      margin-bottom: 0;
    }
    .preview-body pre {
      background: rgba(13, 17, 23, 0.85);
      border-radius: 4px;
      padding: 0.5rem;
      overflow-x: auto;
      font-size: 0.8125rem;
    }
    .preview-footer {
      flex-shrink: 0;
      padding: 0.75rem 1rem;
      border-top: 1px solid rgba(240, 246, 252, 0.1);
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 0.5rem;
      background: rgba(22, 27, 34, 0.4);
    }
    /* Delete and the browse-only note, held at the left end of the
     * footer so the two session actions keep the right end to
     * themselves. */
    .footer-left {
      margin-right: auto;
      display: flex;
      align-items: center;
      gap: 0.5rem;
      min-width: 0;
    }
    .footer-note {
      font-size: 0.75rem;
      color: #d29922;
    }
    /* Delete. As far from Resume as the row allows, and unfilled
     * until it is armed — the second click is the one that destroys
     * a transcript, so that is the click the red is for. */
    .delete-button {
      padding: 0.4rem 0.75rem;
      background: transparent;
      border: 1px solid rgba(248, 81, 73, 0.35);
      border-radius: 4px;
      color: #f85149;
      font-family: inherit;
      font-size: 0.8125rem;
      cursor: pointer;
    }
    .delete-button:hover {
      background: rgba(248, 81, 73, 0.1);
      border-color: #f85149;
    }
    .delete-button.armed {
      background: #f85149;
      border-color: #f85149;
      color: #0d1117;
      font-weight: 600;
    }
    .delete-button:disabled {
      opacity: 0.4;
      cursor: not-allowed;
    }
    .load-button {
      padding: 0.4rem 1rem;
      background: var(--accent-primary, #58a6ff);
      border: none;
      border-radius: 4px;
      color: #0d1117;
      font-weight: 600;
      cursor: pointer;
      font-size: 0.875rem;
    }
    .load-button:hover {
      filter: brightness(1.1);
    }
    .load-button:disabled {
      opacity: 0.4;
      cursor: not-allowed;
    }
    /* Fork. Outlined rather than filled: it sits beside Resume and
     * only one of the two can be the default action. */
    .load-button.secondary {
      background: transparent;
      border: 1px solid rgba(240, 246, 252, 0.25);
      color: var(--text-primary, #c9d1d9);
    }
    .load-button.secondary:hover {
      border-color: var(--accent-primary, #58a6ff);
      background: rgba(88, 166, 255, 0.08);
    }
    .search-hit {
      padding: 0.6rem 0.75rem;
      cursor: pointer;
      border-bottom: 1px solid rgba(240, 246, 252, 0.05);
    }
    .search-hit:hover {
      background: rgba(240, 246, 252, 0.04);
    }
    .search-hit .hit-role {
      font-size: 0.7rem;
      font-weight: 600;
      text-transform: uppercase;
      color: var(--accent-primary, #58a6ff);
      margin-bottom: 0.2rem;
    }
    .search-hit .hit-content {
      font-size: 0.8125rem;
      overflow: hidden;
      text-overflow: ellipsis;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
    }
    /* Message action buttons — mirrors the chat panel's
     * hover toolbar pattern. Position:relative on the card
     * plus absolute on the toolbar keeps the buttons
     * anchored to the top-right regardless of message
     * length. Hover-only via opacity so they don't clutter
     * the reading view. */
    .preview-message {
      position: relative;
    }
    .preview-toolbar {
      position: absolute;
      top: 0.35rem;
      right: 0.35rem;
      display: flex;
      gap: 0.2rem;
      opacity: 0;
      transition: opacity 120ms ease;
      z-index: 1;
    }
    .preview-message:hover .preview-toolbar {
      opacity: 1;
    }
    .preview-action-button {
      background: rgba(13, 17, 23, 0.85);
      border: 1px solid rgba(240, 246, 252, 0.2);
      color: var(--text-primary, #c9d1d9);
      padding: 0.1rem 0.35rem;
      font-size: 0.7rem;
      border-radius: 3px;
      cursor: pointer;
      line-height: 1;
    }
    .preview-action-button:hover {
      background: rgba(240, 246, 252, 0.1);
      border-color: rgba(240, 246, 252, 0.4);
    }
    /* Image thumbnails in history preview messages.
     * Smaller than the chat panel's thumbnails (60px vs
     * 80px) because the preview pane is narrower and
     * users are scanning, not interacting. No re-attach
     * overlay — past-session images aren't part of the
     * current composition flow. */
    .preview-images {
      display: flex;
      flex-wrap: wrap;
      gap: 0.4rem;
      margin-top: 0.4rem;
    }
    .preview-image {
      width: 60px;
      height: 60px;
      object-fit: cover;
      border-radius: 3px;
      border: 1px solid rgba(240, 246, 252, 0.15);
      display: block;
    }
    /* A pointer that has not been resolved yet, and one that never
     * will be. Both hold the same 60px box the image will occupy, so
     * a session full of screenshots does not reflow line by line as
     * they arrive. */
    .preview-image-pending,
    .preview-image-missing {
      width: 60px;
      height: 60px;
      border-radius: 3px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 1.2rem;
    }
    .preview-image-pending {
      border: 1px dashed rgba(240, 246, 252, 0.2);
      background: rgba(240, 246, 252, 0.04);
      opacity: 0.5;
    }
    .preview-image-missing {
      border: 1px solid rgba(248, 81, 73, 0.35);
      background: rgba(248, 81, 73, 0.08);
      cursor: help;
    }
    /* Edit-block cards — mirrored from chat-panel so
     * historical assistant messages render edit blocks
     * the same way the live chat does. Shadow-DOM
     * scoping means we need a copy of every rule the
     * card markup depends on; sharing a stylesheet
     * between two custom elements would need adopted
     * stylesheets, which is heavier than just
     * duplicating ~90 lines of CSS. */
    .preview-body .edit-block-card {
      border: 1px solid rgba(240, 246, 252, 0.15);
      border-radius: 6px;
      background: rgba(13, 17, 23, 0.4);
      overflow: hidden;
      font-size: 0.875rem;
      margin: 0.5rem 0;
    }
    .preview-body .edit-block-card.edit-status-applied {
      border-color: rgba(126, 231, 135, 0.4);
    }
    .preview-body .edit-block-card.edit-status-failed {
      border-color: rgba(248, 81, 73, 0.45);
    }
    .preview-body .edit-block-card.edit-status-skipped,
    .preview-body .edit-block-card.edit-status-not-in-context {
      border-color: rgba(210, 153, 34, 0.4);
    }
    .preview-body .edit-block-card.edit-status-pending,
    .preview-body .edit-block-card.edit-status-new {
      border-color: rgba(88, 166, 255, 0.35);
    }
    .preview-body .edit-card-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.5rem;
      padding: 0.4rem 0.75rem;
      background: rgba(22, 27, 34, 0.7);
      border-bottom: 1px solid rgba(240, 246, 252, 0.08);
    }
    .preview-body .edit-file-path {
      font-family: 'SFMono-Regular', Consolas, monospace;
      font-size: 0.8125rem;
      color: var(--accent-primary, #58a6ff);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      padding: 0.1rem 0.25rem;
      margin: -0.1rem -0.25rem;
      border-radius: 3px;
    }
    .preview-body .edit-status-badge {
      display: inline-flex;
      align-items: center;
      gap: 0.3rem;
      flex-shrink: 0;
      font-size: 0.75rem;
      padding: 0.1rem 0.4rem;
      border-radius: 3px;
      background: rgba(13, 17, 23, 0.6);
    }
    .preview-body .edit-status-icon {
      font-size: 0.875rem;
    }
    .preview-body .edit-status-applied {
      color: #7ee787;
    }
    .preview-body .edit-status-failed {
      color: #f85149;
    }
    .preview-body .edit-status-skipped,
    .preview-body .edit-status-not-in-context {
      color: #d29922;
    }
    .preview-body .edit-status-pending,
    .preview-body .edit-status-new {
      color: var(--accent-primary, #58a6ff);
    }
    .preview-body .edit-status-unknown {
      color: var(--text-secondary, #8b949e);
    }
    .preview-body .edit-body {
      display: flex;
      flex-direction: column;
    }
    .preview-body .edit-pane-content {
      margin: 0;
      padding: 0.5rem 0.75rem;
      background: transparent;
      border: none;
      border-radius: 0;
      overflow-x: auto;
      font-family: 'SFMono-Regular', Consolas, monospace;
      font-size: 0.8125rem;
      line-height: 1.45;
      color: var(--text-primary, #c9d1d9);
    }
    .preview-body .diff-line {
      display: block;
      white-space: pre;
      padding: 0 0.25rem;
      margin: 0 -0.25rem;
      border-left: 2px solid transparent;
    }
    .preview-body .diff-line.context {
      color: var(--text-primary, #c9d1d9);
    }
    .preview-body .diff-line.add {
      background: rgba(126, 231, 135, 0.12);
      border-left-color: rgba(126, 231, 135, 0.5);
      color: #a6e3af;
    }
    .preview-body .diff-line.remove {
      background: rgba(248, 81, 73, 0.12);
      border-left-color: rgba(248, 81, 73, 0.5);
      color: #ff9b93;
    }
    .preview-body .diff-prefix {
      display: inline-block;
      width: 1em;
      user-select: none;
      opacity: 0.55;
      margin-right: 0.25rem;
    }
    .preview-body .diff-text {
      display: inline;
    }
    .preview-body .diff-change {
      border-radius: 2px;
      padding: 0 1px;
    }
    .preview-body .diff-line.add .diff-change {
      background: rgba(126, 231, 135, 0.35);
      color: #fff;
    }
    .preview-body .diff-line.remove .diff-change {
      background: rgba(248, 81, 73, 0.35);
      color: #fff;
    }
    .preview-body .edit-error-message {
      padding: 0.4rem 0.75rem;
      background: rgba(248, 81, 73, 0.08);
      color: #f85149;
      font-size: 0.8125rem;
      border-top: 1px solid rgba(248, 81, 73, 0.15);
    }
    /* Context menu — fixed position at the click point.
     * Appears above the modal via z-index. Dismiss logic
     * lives in the document click handler. */
    .context-menu {
      position: fixed;
      z-index: 200;
      background: rgba(22, 27, 34, 0.98);
      border: 1px solid rgba(240, 246, 252, 0.2);
      border-radius: 4px;
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.5);
      padding: 0.25rem 0;
      min-width: 200px;
      display: flex;
      flex-direction: column;
    }
    .context-menu-item {
      background: transparent;
      border: none;
      color: var(--text-primary, #c9d1d9);
      text-align: left;
      padding: 0.4rem 0.75rem;
      font-size: 0.8125rem;
      font-family: inherit;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }
    .context-menu-item:hover {
      background: rgba(88, 166, 255, 0.12);
    }
    .context-menu-icon {
      flex-shrink: 0;
      width: 1rem;
      text-align: center;
    }
  `;

  constructor() {
    super();
    this.open = false;
    this._sessions = [];
    this._loadingSessions = false;
    this._listError = '';
    this._selectedSessionId = null;
    this._selectedMessages = [];
    this._loadingMessages = false;
    this._messagesError = '';
    this._searchQuery = '';
    this._searchMode = false;
    this._searchHits = [];
    this._searchError = '';
    this._loadingSession = null;
    this._confirmDelete = null;
    this._deleting = false;
    this._contextMenu = null;

    // Debounce timer for search.
    this._searchDebounceTimer = null;
    // Generation counter to discard stale RPC responses
    // when the user types faster than the server responds.
    this._searchGeneration = 0;
    this._messagesGeneration = 0;
    // Resolved image pointers, keyed by `imageKey`. Not a reactive
    // property: it is written one entry at a time from a loop that
    // calls `requestUpdate()` itself, and replacing the whole Map per
    // image so Lit could see the change would be a lie about what
    // changed.
    this._images = new Map();

    this._onKeyDown = this._onKeyDown.bind(this);
    this._onContextDismiss = this._onContextDismiss.bind(this);
    this._onSessionDeleted = this._onSessionDeleted.bind(this);
  }

  connectedCallback() {
    super.connectedCallback();
    // Listen on document so Escape works regardless of
    // which element has focus inside the modal.
    document.addEventListener('keydown', this._onKeyDown);
    // A session can be deleted by another client, or by this one.
    // Either way the row has to go, and the broadcast is the only
    // account of it that reaches every open list.
    window.addEventListener('session-deleted', this._onSessionDeleted);
    // Dismiss context menu on any click outside it.
    // Using `click` (not `pointerdown`) so clicks on
    // menu buttons fire their own handlers before this
    // dismiss runs. Capture phase so we see clicks that
    // bubble up through the shadow DOM.
    document.addEventListener('click', this._onContextDismiss);
  }

  disconnectedCallback() {
    document.removeEventListener('keydown', this._onKeyDown);
    document.removeEventListener(
      'click',
      this._onContextDismiss,
    );
    window.removeEventListener(
      'session-deleted',
      this._onSessionDeleted,
    );
    if (this._searchDebounceTimer != null) {
      clearTimeout(this._searchDebounceTimer);
      this._searchDebounceTimer = null;
    }
    super.disconnectedCallback();
  }

  updated(changedProps) {
    // When the modal opens, load the session list. When it
    // closes, reset transient state so the next open starts
    // fresh (don't carry over stale search, selection, etc.).
    if (changedProps.has('open')) {
      if (this.open) {
        // Defer the fetch to the next microtask so property
        // mutations inside `_loadSessions` (loading flag,
        // sessions array) happen OUTSIDE the update cycle.
        // Setting reactive state inside `updated` triggers
        // Lit's "change-in-update" warning and schedules a
        // redundant update. The microtask hop separates the
        // two phases cleanly.
        Promise.resolve().then(() => {
          // Re-check `open` — the user could have closed
          // the modal in the microsecond between update
          // and microtask. Without this guard we'd issue
          // an RPC for a modal the user already dismissed.
          if (this.open) this._loadSessions();
        });
      } else {
        // Modal closed — reset local state. Preserve the
        // list so a quick close/open doesn't re-fetch;
        // but clear search and selection. Defer to the
        // next microtask so property writes happen outside
        // the update cycle. The initial mount (open goes
        // from undefined → false) also lands here; all
        // five fields are already at their defaults so the
        // microtask is effectively a no-op, but deferring
        // means we never trigger the change-in-update
        // warning.
        Promise.resolve().then(() => {
          if (this.open) return;
          this._searchQuery = '';
          this._searchMode = false;
          this._searchHits = [];
          this._searchError = '';
          this._selectedSessionId = null;
          this._selectedMessages = [];
          this._messagesError = '';
          this._confirmDelete = null;
          this._contextMenu = null;
        });
      }
    }
  }

  // ---------------------------------------------------------------
  // Data loading
  // ---------------------------------------------------------------

  async _loadSessions() {
    if (!this.rpcConnected) return;
    this._loadingSessions = true;
    this._listError = '';
    try {
      const result = await withRpcTimeout(
        this.rpcExtract('ClaudeCodeService.history_list'),
        HISTORY_TIMEOUT_MS,
        'history_list',
      );
      this._listError = historyError(result);
      this._sessions = Array.isArray(result) ? result : [];
    } catch (err) {
      // "Method not found" means the test fixture or a
      // stripped-down backend doesn't expose history. The
      // empty-state placeholder already communicates this
      // to the user; no error-level log needed, and no error
      // banner either — there is nothing wrong to report. Any
      // other failure (network, dropped reply) is the user's
      // to see, because it is the difference between an empty
      // history and an unreadable one.
      const message = err?.message || '';
      if (!message.includes('method not found')) {
        console.error(
          '[history-browser] history_list failed',
          err,
        );
        this._listError = message || 'Could not read the session history';
      }
      this._sessions = [];
    } finally {
      this._loadingSessions = false;
    }
  }

  async _loadSessionMessages(sessionId) {
    if (!this.rpcConnected || !sessionId) return;
    // Generation guard — if the user clicks a different
    // session before this response arrives, the stale
    // response must not overwrite the new selection.
    const gen = ++this._messagesGeneration;
    this._loadingMessages = true;
    this._messagesError = '';
    try {
      const result = await withRpcTimeout(
        this.rpcExtract('ClaudeCodeService.history_load', sessionId),
        HISTORY_TIMEOUT_MS,
        'history_load',
      );
      if (gen !== this._messagesGeneration) return;
      // A session with nothing readable behind it answers
      // `{error}` rather than an empty list, precisely so this
      // pane can say why instead of drawing a conversation that
      // happened and said nothing.
      this._messagesError = historyError(result);
      this._selectedMessages = Array.isArray(result) ? result : [];
      // Deliberately not awaited: the transcript is readable now and
      // the images arrive underneath it. Awaiting here would hold the
      // whole preview behind the last screenshot.
      this._hydrateImages(this._selectedMessages, gen);
    } catch (err) {
      console.error(
        '[history-browser] history_load failed',
        err,
      );
      if (gen === this._messagesGeneration) {
        this._selectedMessages = [];
        this._messagesError =
          err?.message || 'Could not read that session';
      }
    } finally {
      if (gen === this._messagesGeneration) {
        this._loadingMessages = false;
      }
    }
  }

  /**
   * Resolve the image pointers in a loaded session to data URIs.
   *
   * The fetching itself is shared with the chat panel (`image-refs.js`),
   * which reads the same pointers out of a resumed session. What is local
   * to the browser is what makes this work stale: selecting another
   * session, which bumps `_messagesGeneration` mid-loop.
   */
  _hydrateImages(messages, gen) {
    return hydrateImageRefs(this, messages, {
      cache: this._images,
      isStale: () => gen !== this._messagesGeneration,
      label: 'history-browser',
    });
  }

  async _runSearch(query) {
    const gen = ++this._searchGeneration;
    if (!this.rpcConnected) return;
    try {
      const result = await withRpcTimeout(
        this.rpcExtract('ClaudeCodeService.history_search', query),
        HISTORY_TIMEOUT_MS,
        'history_search',
      );
      if (gen !== this._searchGeneration) return;
      this._searchError = historyError(result);
      this._searchHits = Array.isArray(result) ? result : [];
    } catch (err) {
      console.error(
        '[history-browser] history_search failed',
        err,
      );
      if (gen === this._searchGeneration) {
        this._searchHits = [];
        this._searchError =
          err?.message || 'Could not search the session history';
      }
    }
  }

  // ---------------------------------------------------------------
  // User actions
  // ---------------------------------------------------------------

  _onBackdropClick(event) {
    // Only close if the click landed on the backdrop itself,
    // not on the modal. Bubbling clicks from inside the
    // modal shouldn't close it.
    if (event.target === event.currentTarget) {
      this._close();
    }
  }

  _onCloseClick() {
    this._close();
  }

  _close() {
    this._contextMenu = null;
    this.dispatchEvent(
      new CustomEvent('close', { bubbles: true, composed: true }),
    );
  }

  _onSearchInput(event) {
    const value = event.target.value;
    this._searchQuery = value;
    // Debounce — cancel any pending timer and start fresh.
    if (this._searchDebounceTimer != null) {
      clearTimeout(this._searchDebounceTimer);
      this._searchDebounceTimer = null;
    }
    const trimmed = value.trim();
    if (!trimmed) {
      // Empty query → exit search mode, show session list.
      this._searchMode = false;
      this._searchHits = [];
      this._searchError = '';
      // Bump the generation so any in-flight response gets
      // discarded.
      this._searchGeneration += 1;
      return;
    }
    this._searchMode = true;
    this._searchDebounceTimer = setTimeout(() => {
      this._searchDebounceTimer = null;
      this._runSearch(trimmed);
    }, SEARCH_DEBOUNCE_MS);
  }

  _onSearchKeyDown(event) {
    if (event.key === 'Escape') {
      // Two-step Escape: clear the query first, close only
      // if the query is already empty. Matches specs — gives
      // the user a single key to both clear and close.
      event.stopPropagation();
      if (this._searchQuery) {
        this._searchQuery = '';
        this._searchMode = false;
        this._searchHits = [];
        this._searchError = '';
        this._searchGeneration += 1;
        if (this._searchDebounceTimer != null) {
          clearTimeout(this._searchDebounceTimer);
          this._searchDebounceTimer = null;
        }
      } else {
        this._close();
      }
    }
  }

  _onKeyDown(event) {
    if (!this.open) return;
    if (event.key === 'Escape') {
      // If the context menu is open, close it first.
      // Second Escape then closes the modal via the
      // normal path.
      if (this._contextMenu) {
        event.stopPropagation();
        this._contextMenu = null;
        return;
      }
      // Top-level Escape handler — closes if the search
      // input didn't already handle it. The search input's
      // handler calls stopPropagation when it's in play.
      this._close();
    }
  }

  _onContextDismiss(event) {
    if (!this._contextMenu) return;
    // Check whether the click landed inside the context
    // menu itself. If so, let the button's own handler
    // run (it'll close the menu as part of its action).
    // composedPath traverses shadow boundaries, which we
    // need because the menu lives in our shadow DOM.
    const path = event.composedPath ? event.composedPath() : [];
    for (const el of path) {
      if (
        el &&
        el.classList &&
        el.classList.contains('context-menu')
      ) {
        return;
      }
    }
    this._contextMenu = null;
  }

  _onSessionClick(sessionId) {
    if (this._selectedSessionId === sessionId) return;
    this._selectedSessionId = sessionId;
    this._selectedMessages = [];
    this._messagesError = '';
    // An armed Delete belongs to the session it was armed on. Moving
    // the selection disarms it rather than re-aiming it.
    this._confirmDelete = null;
    this._loadSessionMessages(sessionId);
  }

  _onSearchHitClick(sessionId) {
    // A search hit is a message in a session — clicking it
    // selects that session for preview. A future enhancement
    // could scroll to the specific message; for now we just
    // load the session.
    this._searchMode = false;
    this._searchQuery = '';
    this._onSessionClick(sessionId);
  }

  /**
   * The row the footer buttons act on, or undefined when the
   * selection is not in the current listing.
   */
  _selectedSession() {
    return this._sessions.find(
      (s) => s.session_id === this._selectedSessionId,
    );
  }

  /**
   * Attach the engine to the selected session, or to a copy of it.
   *
   * `fork` is the difference between continuing that conversation and
   * branching off it: a fork leaves the original untouched, so it is
   * the safe half of this pair and the reason both buttons exist.
   *
   * Nothing is read back into a prompt. The RPC hands the session ID
   * to the CLI, which rebuilds its own context from the transcript,
   * and the server broadcasts `sessionChanged` with the rendered
   * messages — that broadcast is what repopulates the chat panel. The
   * local event below only tells the parent to close the modal.
   */
  async _onResumeClick(fork = false) {
    if (!this._selectedSessionId) return;
    if (!this.rpcConnected) return;
    if (this._loadingSession) return;
    if (!isResumable(this._selectedSession())) return;
    const action = fork ? 'forked' : 'resumed';
    this._loadingSession = action;
    try {
      const result = await withRpcTimeout(
        this.rpcExtract(
          'ClaudeCodeService.resume_session',
          this._selectedSessionId,
          fork,
        ),
        HISTORY_TIMEOUT_MS,
        'resume_session',
      );
      if (result && result.error) {
        // A refusal, not a crash: a turn is still running, the
        // caller is a remote participant, or the transcript cannot
        // be read. Each is a sentence the user can act on, so it
        // goes to the toast layer verbatim and the modal stays open
        // on the session they picked.
        this._emitToast(String(result.error), 'warning');
        return;
      }
      this.dispatchEvent(
        new CustomEvent('session-loaded', {
          detail: {
            // The session the user acted on. The engine's new ID is
            // minted by the CLI and only arrives with the first
            // turn — for a fork there is no other ID yet — so this
            // is the only one that means anything here.
            session_id: this._selectedSessionId,
            action,
          },
          bubbles: true,
          composed: true,
        }),
      );
    } catch (err) {
      console.error('[history-browser] resume_session failed', err);
      this._emitToast(
        err?.message || 'Could not open that session',
        'warning',
      );
    } finally {
      this._loadingSession = null;
    }
  }

  /**
   * Delete the selected session — first click arms, second confirms.
   *
   * Two clicks because this is the only irreversible thing in the
   * modal: the transcript is the one file under `.ac-dc4/` that does
   * not rebuild, and it takes the session's pasted images, its
   * operational events and its index rows with it. An arming step is
   * cheaper than an undo that cannot exist.
   *
   * The row is not removed here. `history_delete` broadcasts
   * `sessionDeleted` to every client, and this component drops the
   * row when it arrives — so the list this browser shows is the same
   * list every other open browser shows, by the same route.
   */
  async _onDeleteClick() {
    const sessionId = this._selectedSessionId;
    if (!sessionId) return;
    if (!this.rpcConnected) return;
    if (this._deleting) return;
    if (this._confirmDelete !== sessionId) {
      this._confirmDelete = sessionId;
      return;
    }
    this._deleting = true;
    try {
      const result = await withRpcTimeout(
        this.rpcExtract('ClaudeCodeService.history_delete', sessionId),
        HISTORY_TIMEOUT_MS,
        'history_delete',
      );
      if (result && result.error) {
        // The commonest refusal is `session_live`: the store is a
        // live mirror, so deleting the conversation on screen would
        // see it written straight back. The message says to start a
        // new session first, which is the way out.
        this._emitToast(String(result.error), 'warning');
        return;
      }
      this._emitToast('Session deleted', 'success');
    } catch (err) {
      console.error('[history-browser] history_delete failed', err);
      this._emitToast(
        err?.message || 'Could not delete that session',
        'warning',
      );
    } finally {
      this._deleting = false;
      this._confirmDelete = null;
    }
  }

  /**
   * A session went away — deleted here, or by another client.
   *
   * Drops the row and any search hits pointing at it. If it was the
   * one being previewed, the selection goes too: a preview of a
   * transcript that no longer exists is the same lie as a row that
   * offers to resume it.
   */
  _onSessionDeleted(event) {
    const sessionId = event?.detail?.session_id;
    if (!sessionId) return;
    this._sessions = this._sessions.filter(
      (s) => s.session_id !== sessionId,
    );
    this._searchHits = this._searchHits.filter(
      (hit) => hit.session_id !== sessionId,
    );
    if (this._confirmDelete === sessionId) this._confirmDelete = null;
    if (this._selectedSessionId === sessionId) {
      this._selectedSessionId = null;
      this._selectedMessages = [];
      this._messagesError = '';
      // Any load still in flight for it must not paint into the
      // pane it no longer owns.
      this._messagesGeneration += 1;
      this._loadingMessages = false;
    }
  }

  // ---------------------------------------------------------------
  // Message actions (copy, paste-to-prompt, load-in-panel)
  // ---------------------------------------------------------------

  /**
   * Extract raw text from a message, handling both string
   * and multimodal-array content shapes. Images are
   * dropped — text actions only. Mirrors the chat panel's
   * _extractMessageText helper.
   */
  _extractMessageText(msg) {
    if (!msg) return '';
    const normalized = normalizeMessageContent(msg);
    return normalized.content;
  }

  /**
   * Copy a message's raw text to the clipboard. Emits a
   * toast on success via the ac-toast window event so the
   * app shell's toast layer can render it — the history
   * browser is modal, so a local toast layer here would
   * be overkill.
   */
  async _copyMessageText(msg) {
    const text = this._extractMessageText(msg);
    if (!text) return;
    try {
      if (
        navigator.clipboard &&
        typeof navigator.clipboard.writeText === 'function'
      ) {
        await navigator.clipboard.writeText(text);
        this._emitToast('Copied to clipboard', 'success');
      } else {
        this._emitToast('Clipboard not available', 'warning');
      }
    } catch (err) {
      this._emitToast(
        `Copy failed: ${err?.message || 'permission denied'}`,
        'warning',
      );
    }
  }

  /**
   * Paste a message's text into the chat input. Dispatches
   * a `paste-to-prompt` event with the text in detail; the
   * chat panel catches it and inserts at the cursor. Closes
   * the modal after so the user sees their prompt area.
   */
  _pasteMessageToPrompt(msg) {
    const text = this._extractMessageText(msg);
    if (!text) return;
    this.dispatchEvent(
      new CustomEvent('paste-to-prompt', {
        detail: { text },
        bubbles: true,
        composed: true,
      }),
    );
    this._close();
  }

  /**
   * Load a message's content into a diff-viewer panel for
   * ad-hoc comparison. Dispatches `load-diff-panel` with
   * `{content, panel}` — chat panel forwards it to the diff
   * viewer's loadPanel method. Panel is 'left' or 'right'.
   * Does not close the modal — user may want to load a
   * second message into the other panel.
   */
  _loadMessageInPanel(msg, panel) {
    const text = this._extractMessageText(msg);
    if (!text) return;
    const label = msg.role
      ? `${msg.role} (history)`
      : 'history';
    this.dispatchEvent(
      new CustomEvent('load-diff-panel', {
        detail: { content: text, panel, label },
        bubbles: true,
        composed: true,
      }),
    );
    this._contextMenu = null;
  }

  _emitToast(message, type = 'info') {
    window.dispatchEvent(
      new CustomEvent('ac-toast', {
        detail: { message, type },
        bubbles: false,
      }),
    );
  }

  // ---------------------------------------------------------------
  // Context menu
  // ---------------------------------------------------------------

  _onMessageContextMenu(event, msg) {
    event.preventDefault();
    // Position in viewport coordinates. The menu renders
    // position:fixed so these coordinates map directly.
    this._contextMenu = {
      x: event.clientX,
      y: event.clientY,
      message: msg,
    };
  }

  // ---------------------------------------------------------------
  // Rendering
  // ---------------------------------------------------------------

  render() {
    if (!this.open) return html``;
    return html`
      <div
        class="backdrop"
        @click=${this._onBackdropClick}
        role="presentation"
      >
        <div
          class="modal"
          role="dialog"
          aria-modal="true"
          aria-label="History browser"
        >
          <div class="modal-header">
            <div class="modal-title">History</div>
            <input
              type="text"
              class="search-input"
              placeholder="Search messages…"
              .value=${this._searchQuery}
              @input=${this._onSearchInput}
              @keydown=${this._onSearchKeyDown}
              aria-label="Search history"
            />
            <button
              class="close-button"
              @click=${this._onCloseClick}
              aria-label="Close history browser"
              title="Close (Escape)"
            >
              ✕
            </button>
          </div>
          <div class="modal-body">
            <div class="sessions-pane">
              ${this._searchMode
                ? this._renderSearchHits()
                : this._renderSessionList()}
            </div>
            <div class="preview-pane">
              <div class="preview-messages">
                ${this._renderPreview()}
              </div>
              <div class="preview-footer">
                ${this._renderResumeControls()}
              </div>
            </div>
          </div>
        </div>
        ${this._renderContextMenu()}
      </div>
    `;
  }

  /**
   * The footer's two session actions. Fork is rendered beside Resume
   * rather than behind a menu: it is the choice that cannot damage the
   * original, and the native engine had no equivalent, so it is worth
   * surfacing wherever resume is (specs5/3-engine/history.md).
   */
  _renderResumeControls() {
    const selected = this._selectedSession();
    const resumable = isResumable(selected);
    const blocked =
      !this._selectedSessionId ||
      this._loadingSession != null ||
      !resumable ||
      !this.rpcConnected;
    const armed = this._confirmDelete === this._selectedSessionId;
    return html`
      <div class="footer-left">
        <button
          class="delete-button ${armed ? 'armed' : ''}"
          ?disabled=${!this._selectedSessionId ||
          this._deleting ||
          !this.rpcConnected}
          @click=${() => this._onDeleteClick()}
          title=${armed
            ? 'Click again to delete this session permanently'
            : 'Delete this session, its images and its events'}
        >
          ${this._deleting
            ? 'Deleting…'
            : armed
              ? 'Delete permanently?'
              : '🗑 Delete'}
        </button>
        ${this._selectedSessionId && !resumable
          ? html`<span class="footer-note">
              No engine transcript survives — browsable only
            </span>`
          : ''}
      </div>
      <button
        class="load-button secondary fork-button"
        ?disabled=${blocked}
        @click=${() => this._onResumeClick(true)}
        title="Branch a copy; the original session stays intact"
      >
        ${this._loadingSession === 'forked' ? 'Forking…' : 'Fork'}
      </button>
      <button
        class="load-button resume-button"
        ?disabled=${blocked}
        @click=${() => this._onResumeClick(false)}
        title="Attach the engine to this session"
      >
        ${this._loadingSession === 'resumed' ? 'Resuming…' : 'Resume'}
      </button>
    `;
  }

  _renderSessionList() {
    if (this._loadingSessions) {
      return html`<div class="loading-note">Loading sessions…</div>`;
    }
    if (this._listError) {
      return html`<div class="error-note" role="alert">
        ${this._listError}
      </div>`;
    }
    if (this._sessions.length === 0) {
      return html`<div class="empty-list">No sessions yet</div>`;
    }
    return this._sessions.map(
      (s) => html`
        <div
          class="session-item ${this._selectedSessionId === s.session_id
            ? 'selected'
            : ''}"
          @click=${() => this._onSessionClick(s.session_id)}
          role="button"
          aria-pressed=${this._selectedSessionId === s.session_id}
        >
          <div class="session-preview">
            ${s.preview || '(empty)'}
          </div>
          <div class="session-meta">
            <span>${formatRelativeTime(s.timestamp)}</span>
            <span class="msg-count">
              ${s.message_count} ${s.message_count === 1 ? 'msg' : 'msgs'}
            </span>
            ${isResumable(s)
              ? ''
              : html`<span
                  class="not-resumable"
                  title="No engine transcript survives for this session"
                  >browse only</span
                >`}
          </div>
        </div>
      `,
    );
  }

  _renderSearchHits() {
    if (this._searchError) {
      return html`<div class="error-note" role="alert">
        ${this._searchError}
      </div>`;
    }
    if (this._searchHits.length === 0) {
      return html`<div class="empty-list">
        ${this._searchQuery.trim()
          ? 'No matches'
          : 'Type to search'}
      </div>`;
    }
    return this._searchHits.map((hit) => {
      // Hit shape from history_search: {session_id,
      // entry_uuid, role, content_preview, timestamp}. `role` is
      // one of user/assistant/tool — that third value is new, and
      // is what makes a search for a path or a command find the
      // tool call that used it.
      const preview = hit.content_preview || hit.content || '';
      return html`
        <div
          class="search-hit"
          @click=${() => this._onSearchHitClick(hit.session_id)}
          role="button"
        >
          <div class="hit-role">
            ${hit.role || 'message'}
            · ${formatRelativeTime(hit.timestamp)}
          </div>
          <div class="hit-content">${preview}</div>
        </div>
      `;
    });
  }

  _renderPreview() {
    if (!this._selectedSessionId) {
      return html`<div class="preview-empty">
        Select a session to preview
      </div>`;
    }
    if (this._loadingMessages) {
      return html`<div class="preview-empty">Loading messages…</div>`;
    }
    if (this._messagesError) {
      return html`<div class="error-note" role="alert">
        ${this._messagesError}
      </div>`;
    }
    if (this._selectedMessages.length === 0) {
      return html`<div class="preview-empty">Empty session</div>`;
    }
    return this._selectedMessages.map((msg) =>
      this._renderPreviewMessage(msg),
    );
  }

  _renderPreviewMessage(msg) {
    const roleClass = msg.system_event
      ? 'role-system'
      : `role-${msg.role || 'assistant'}`;
    const roleLabel = msg.system_event
      ? 'System'
      : msg.role === 'user'
        ? 'You'
        : msg.role === 'assistant'
          ? 'Assistant'
          : msg.role || 'Message';
    // Normalize content for both text rendering and
    // image extraction. Multimodal arrays come from
    // callers that hand us a raw message shape; the
    // normalizer strips text and images into clean
    // fields. A pre-existing `images` field of data URIs
    // takes precedence over extraction.
    const normalized = normalizeMessageContent(msg);
    const textContent =
      typeof msg.content === 'string'
        ? msg.content
        : normalized.content;
    const images = Array.isArray(msg.images)
      ? msg.images
      : normalized.images;
    // Pointers are the shape `history_load` renders; the data-URI
    // array above is what a live paste hands us in the same turn. Both
    // can be present on the same message and neither displaces the
    // other, so they are drawn as two groups rather than merged.
    const refs = imageRefsOf(msg);
    // For assistant messages, segment the response so edit
    // blocks render as visual cards rather than as a wall
    // of marker-laden prose. Past sessions carry no live
    // `edit_results` — segments resolve to `pending` (modify
    // blocks) or `new` (create blocks) status, which is the
    // honest signal: we can't tell what happened at the time.
    //
    // User and system messages have no edit blocks, so they
    // skip the segmentation hop and render straight through
    // markdown.
    let body;
    if (msg.role === 'assistant' && typeof textContent === 'string') {
      const segments = segmentResponse(textContent);
      const editResults = Array.isArray(msg.edit_results)
        ? msg.edit_results
        : [];
      const matched = matchSegmentsToResults(segments, editResults);
      body = html`${segments.map((seg, i) => {
        if (seg.type === 'text') {
          return html`${unsafeHTML(renderMarkdown(seg.content))}`;
        }
        return html`${unsafeHTML(renderEditCard(seg, matched[i]))}`;
      })}`;
    } else {
      // All other roles go through the markdown renderer so
      // user newlines and lists render the same way they do
      // in the main chat panel. The renderer handles escaping
      // internally, so this is safe against HTML injection.
      body = html`${unsafeHTML(renderMarkdown(textContent))}`;
    }
    return html`
      <div
        class="preview-message ${roleClass}"
        @contextmenu=${(e) => this._onMessageContextMenu(e, msg)}
      >
        <div class="preview-toolbar">
          <button
            class="preview-action-button"
            title="Copy raw text"
            aria-label="Copy message text"
            @click=${(e) => {
              e.stopPropagation();
              this._copyMessageText(msg);
            }}
          >
            📋
          </button>
          <button
            class="preview-action-button"
            title="Paste into input"
            aria-label="Paste text into chat input"
            @click=${(e) => {
              e.stopPropagation();
              this._pasteMessageToPrompt(msg);
            }}
          >
            ↩
          </button>
        </div>
        <div class="preview-role-label">${roleLabel}</div>
        <div class="preview-body">${body}</div>
        ${images.length > 0
          ? this._renderPreviewImages(images)
          : ''}
        ${refs.length > 0 ? this._renderImageRefs(refs) : ''}
      </div>
    `;
  }

  _renderPreviewImages(images) {
    return html`
      <div class="preview-images" role="list">
        ${images.map(
          (dataUri) => html`
            <img
              class="preview-image"
              src=${dataUri}
              alt=""
              role="listitem"
            />
          `,
        )}
      </div>
    `;
  }

  /**
   * One tile per image pointer, in whichever of its three states it
   * is in: waiting on `history_image`, resolved to bytes, or gone.
   *
   * A failure gets a tile of its own rather than nothing. An image
   * silently absent from a prompt reads as a prompt that never had one,
   * which is a different conversation from the one that happened.
   */
  _renderImageRefs(refs) {
    return html`
      <div class="preview-images" role="list">
        ${refs.map((ref) => {
          const entry = this._images.get(imageRefKey(ref));
          if (entry?.dataUri) {
            return html`
              <img
                class="preview-image"
                src=${entry.dataUri}
                alt=""
                role="listitem"
              />
            `;
          }
          if (entry?.error) {
            return html`
              <div
                class="preview-image-missing"
                role="listitem"
                title=${entry.error}
              >
                🚫
              </div>
            `;
          }
          return html`
            <div
              class="preview-image-pending"
              role="listitem"
              title=${ref.media_type || 'image'}
            >
              🖼
            </div>
          `;
        })}
      </div>
    `;
  }

  _renderContextMenu() {
    if (!this._contextMenu) return '';
    const { x, y, message } = this._contextMenu;
    // Position in viewport coords. Using style= here
    // rather than a CSS class because the coordinates
    // are dynamic per click.
    const style = `left: ${x}px; top: ${y}px;`;
    return html`
      <div class="context-menu" style=${style} role="menu">
        <button
          class="context-menu-item"
          role="menuitem"
          @click=${() => this._loadMessageInPanel(message, 'left')}
        >
          <span class="context-menu-icon">◧</span>
          Load in Left Panel
        </button>
        <button
          class="context-menu-item"
          role="menuitem"
          @click=${() => this._loadMessageInPanel(message, 'right')}
        >
          <span class="context-menu-icon">◨</span>
          Load in Right Panel
        </button>
        <button
          class="context-menu-item"
          role="menuitem"
          @click=${() => {
            this._copyMessageText(message);
            this._contextMenu = null;
          }}
        >
          <span class="context-menu-icon">📋</span>
          Copy
        </button>
        <button
          class="context-menu-item"
          role="menuitem"
          @click=${() => this._pasteMessageToPrompt(message)}
        >
          <span class="context-menu-icon">↩</span>
          Paste to Prompt
        </button>
      </div>
    `;
  }
}

customElements.define('ac-history-browser', HistoryBrowser);

// Exported for tests.
export { formatRelativeTime, SEARCH_DEBOUNCE_MS };