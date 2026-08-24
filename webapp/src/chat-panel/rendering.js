// Render entry point + render helpers for the
// ChatPanel component.
//
// One exported function per visual region of the
// component, plus the top-level `render(panel)`
// that stitches them together. All functions take
// the chat-panel instance as their first argument
// — same convention as tabs.js / search.js /
// streaming.js / input.js.
//
// The split is by visual region rather than by
// state contract, so reading the render code top-
// to-bottom mirrors what the user sees:
//
//   render → tab strip → messages wrapper
//                         → messages list
//                           → renderMessage (per card)
//                             → renderCompactionDivider (boundary marks)
//                             → renderMessageToolbar
//                             → renderTurnBlocks   (Claude Code turns)
//                             → renderAssistantBody (archived prose turns)
//                             → renderTurnFooter   (Claude Code turns)
//                             → renderEditSummary   (archived prose turns)
//                             → renderFileSummary   (archived prose turns)
//                             → renderMessageImages
//                             → renderTerminalBadge / renderFinishBadge
//                           → renderStreamingMessage
//                         → renderFileSearchOverlay
//                       → renderHealthBanner
//                       → disconnected-note
//                       → input-area
//                         → action bar
//                           → renderPermissionModeSelector
//                         → renderSearchBar
//                         → renderInputSurface
//                           → renderSnippetDrawer
//                           → input-history
//                           → renderPendingImages
//                           → input row + send column
//                         → renderReadOnlyNote (read-only tabs instead)
//                       → aic-history-browser (modal)
//                       → renderLightbox
//
// Assistant cards render one of two ways. A turn produced by Claude Code carries
// a `blocks` array and goes through `renderTurnBlocks` / `renderTurnFooter`; a
// turn carrying prose instead goes through `renderAssistantBody` /
// `renderEditSummary`.
//
// The phase-3 plan called for deleting the second path with the engine that
// produced it. It stays, deliberately. Nothing *produces* prose turns any more,
// but two things still *decode* them: `history-browser.js` reads pre-conversion
// session files, and `onSessionChanged` (events.js) normalises every restored
// message to `{role, content}` with no `blocks` at all. Deleting the branch
// would render every archived transcript — and every restored session until
// phase 5 changes that shape — as a wall of raw 🟧🟧🟧 edit markers. A parser
// with no producer but real data to read is not dead code.
//
// Helpers like `collectMessageFiles` and
// `proseContainsPath` are kept here too because
// they only feed into renderFileSummary — moving
// them elsewhere would just create another
// import cycle. They're pure and tested via the
// component's own tests.

import { html } from 'lit';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';

import {
  matchSegmentsToResults,
  segmentResponse,
} from '../edit-blocks.js';
import { renderEditCard } from '../edit-block-render.js';
import { findFileMentions } from '../file-mentions.js';
import { imageRefKey, imageRefsOf } from '../image-refs.js';
import { renderMarkdown } from '../markdown.js';
import {
  compactionSummary,
  renderLiveUsage,
  renderTerminalBadge,
  renderTurnBlocks,
  renderTurnFooter,
  subagentLabel,
  terminalBadge,
} from './block-render.js';
import { isEmptyTurn } from './blocks.js';
import { renderHealthBanner } from './health-banner.js';
import { renderLedRow } from './led-row.js';
import { renderPermissionModeSelector } from './permission-mode.js';
import { renderTabStrip } from './tabs.js';
import { formatRunDuration } from './helpers.js';
import {
  computeSearchMatches,
  onFileSearchNext,
  onFileSearchPrev,
  onFileSearchOverlayScroll,
  onSearchInput,
  onSearchKeyDown,
  onSearchNext,
  onSearchPrev,
  toggleSearchMode,
  toggleSearchOption,
  totalFileSearchMatches,
} from './search.js';
import {
  cancel,
  closeLightbox,
  insertSnippet,
  onFileChipClick,
  onHistoryCancel,
  onHistorySelect,
  onInputChange,
  onInputKeyDown,
  onInputPaste,
  onLightboxKeyDown,
  onMessagesClick,
  onMessagesScroll,
  onSlashCommandSelect,
  openLightbox,
  pasteMessageToPrompt,
  reattachImage,
  removePendingImage,
  send,
  toggleSnippetDrawer,
  copyMessageText,
  speakMessage,
  onNewSession,
  onOpenHistory,
  hasUnreadLiveMessages,
} from './input.js';
import { isSpeechSynthesisSupported } from '../speech-synthesis.js';

// ---------------------------------------------------------------
// What left the action bar in phase 2, and what came back
// ---------------------------------------------------------------
//
// Four controls used to sit on this bar. Each drove the native engine, and each
// would have kept reporting success while changing nothing once the chat path
// moved to Claude Code. That is the exact failure the permission dialog exists
// to prevent — a control whose visible state does not describe what the engine
// will do — so they came off in the same commit that repointed the path rather
// than in phase 3 with the engine itself.
//
// Gone for good:
//
//   💻/📄 + 🔀 — `LLMService.switch_mode` / `set_cross_reference`, choosing
//     which index fed the native engine's context assembly. Claude Code builds
//     its own context by reading files with its own tools; there is no index to
//     switch. The context story came back in phase 3 as `aic-context-usage-tab`
//     and `aic-usage-hud`, which *report* the engine's real usage instead of
//     steering an index we no longer own.
//
//   🧠 + effort — reasoning flags passed as `chat_streaming` arguments the new
//     signature does not take. Worse than merely inert: phase 2 renders thinking
//     blocks, so a 🧠 toggle would read as the switch that controls them.
//
//   `aic-url-chips`, for the same reason at one remove: the chips fetched URLs
//     into the native engine's context. The CLI has WebFetch.
//
// Back in phase 5, now that there is a session lifecycle over the CLI to drive:
//
//   ✨ — `ClaudeCodeService.new_session`. The engine is not reconnected by it;
//     the next turn simply connects with no resume, which is what a new session
//     is. Disabled while streaming because the server refuses mid-turn anyway,
//     and a button that reports a refusal it could have prevented is noise.
//
//   📜 — the history browser, now reading the CLI's own transcript mirrored
//     under `.aic-dc/sessions/` rather than native-engine session files.
//
// The permission-mode selector is the third member of the row and the one
// control that genuinely changes what the next tool call does. It is
// deliberately the first child and deliberately outside every
// `.search-collapsible` group: expanding the search bar must not be able to
// hide the safety posture. The session group *is* collapsible, and is dropped
// outright in file-search mode and on any tab but `main` — a subagent
// transcript has no session of its own to restart, and the ✨ on it would
// restart the conversation that spawned it.

// ---------------------------------------------------------------
// Top-level render
// ---------------------------------------------------------------

/**
 * Render the whole chat panel. Lit calls this on
 * every reactive-property change.
 *
 * Layout sketch:
 *
 *   ┌─ tab strip (hidden when only main exists) ─┐
 *   ├─ messages-wrapper ─────────────────────────┤
 *   │  messages list (or .messages-hidden)        │
 *   │  file-search overlay (when in file mode)   │
 *   ├─ health-banner (when the engine reports) ──┤
 *   ├─ disconnected-note (when RPC down) ────────┤
 *   ├─ input-area ───────────────────────────────┤
 *   │  action-bar (permission mode + search)      │
 *   │  search-bar                                 │
 *   │  snippet drawer (if open)                   │
 *   │  aic-input-history                           │
 *   │  pending images (if any)                    │
 *   │  aic-slash-palette (when typing a /command)  │
 *   │  input row (textarea + send column)         │
 *   ├─ aic-history-browser (modal) ────────────────┤
 *   └─ lightbox (if open) ───────────────────────┘
 *
 * The lightbox lives at component-root level so it
 * can cover the whole shadow root regardless of
 * scroll position inside the messages list.
 *
 * Everything from `aic-input-history` down is absent on a subagent
 * transcript, replaced by one line saying why. Not disabled — absent:
 * "a greyed-out textarea implies a channel that might open under some
 * condition, and none exists" (specs5/5-webapp/subagent-browser.md
 * § Tab Content). The action bar and the LED row stay, because the safety
 * posture and which conversation is live are true on every tab.
 */
export function render(panel) {
  const fileMode = panel._searchMode === 'file';
  const readOnly = !!panel._tabs.get(panel._activeTabId)?.readOnly;
  return html`
    ${renderTabStrip(panel)}
    <div class="messages-wrapper">
      <div
        class="messages ${fileMode ? 'messages-hidden' : ''}"
        role="log"
        aria-live="polite"
        @scroll=${(e) => onMessagesScroll(panel, e)}
        @click=${(e) => onMessagesClick(panel, e)}
      >
        ${panel.messages.length === 0 && !panel._streaming
          ? html`<div class="empty-state">
              Start a conversation…
            </div>`
          : ''}
        ${panel.messages.map((msg, index) =>
          renderMessage(panel, msg, index),
        )}
        ${panel._streaming ? renderStreamingMessage(panel) : ''}
      </div>
      ${fileMode ? renderFileSearchOverlay(panel) : ''}
    </div>
    ${renderHealthBanner(panel)}
    ${!panel.rpcConnected
      ? html`<div class="disconnected-note">
          Not connected to the server
        </div>`
      : ''}
    <div class="input-area">
      <div class="action-bar" role="toolbar">
        ${renderPermissionModeSelector(panel)}
        <div class="action-divider search-collapsible" aria-hidden="true"></div>
        ${renderSearchBar(panel)}
        ${panel._searchMode === 'file' || panel._activeTabId !== 'main'
          ? ''
          : html`
              <div class="action-divider" aria-hidden="true"></div>
              <div class="action-group search-collapsible">
                <button
                  class="action-button new-session-button"
                  ?disabled=${!panel.rpcConnected || panel._streaming}
                  @click=${() => onNewSession(panel)}
                  aria-label="Start a new session"
                  title="New session (clears the conversation)"
                >
                  ✨
                </button>
                <button
                  class="action-button history-button"
                  ?disabled=${!panel.rpcConnected}
                  @click=${() => onOpenHistory(panel)}
                  aria-label="Open history browser"
                  title="Browse past sessions"
                >
                  📜
                </button>
              </div>
            `}
      </div>
      ${readOnly ? renderReadOnlyNote() : renderInputSurface(panel)}
      ${renderLedRow(panel)}
    </div>
    <!--
      liveUnread is the answer to a question only this panel can answer:
      the modal covers the transcript it is about to replace. Bound rather
      than snapshotted at open time, so a collaborator's turn arriving
      behind the modal counts as something the reader has not seen.
    -->
    <aic-history-browser
      ?open=${panel._historyOpen}
      .liveUnread=${hasUnreadLiveMessages(panel)}
      @close=${() => panel._onHistoryClose()}
      @session-loaded=${() => panel._onHistorySessionLoaded()}
    ></aic-history-browser>
    ${panel._lightboxImage ? renderLightbox(panel) : ''}
  `;
}

/**
 * Everything the user composes with: the snippet drawer, the
 * recalled-input list, the attached images and the textarea/send row.
 * Absent on a read-only tab — see `render`'s note.
 */
function renderInputSurface(panel) {
  return html`
    ${panel._snippetDrawerOpen ? renderSnippetDrawer(panel) : ''}
    <aic-input-history
      @history-select=${(e) => onHistorySelect(panel, e)}
      @history-cancel=${(e) => onHistoryCancel(panel, e)}
    ></aic-input-history>
    ${panel._pendingImages.length > 0 ? renderPendingImages(panel) : ''}
    <aic-slash-palette
      @command-select=${(e) => onSlashCommandSelect(panel, e)}
    ></aic-slash-palette>
    <div class="input-row">
      <textarea
        class="input-textarea"
        placeholder="Send a message… (Enter to send, Shift+Enter for newline)"
        .value=${panel._input}
        ?disabled=${!panel.rpcConnected}
        @input=${(e) => onInputChange(panel, e)}
        @keydown=${(e) => onInputKeyDown(panel, e)}
        @paste=${(e) => onInputPaste(panel, e)}
        aria-label="Message input"
      ></textarea>
      <div class="send-column">
        <div class="send-column-top">
          <button
            class="action-button snippet-drawer-button ${panel
              ._snippetDrawerOpen
              ? 'active'
              : ''}"
            @click=${() => toggleSnippetDrawer(panel)}
            aria-label=${panel._snippetDrawerOpen
              ? 'Close snippet drawer'
              : 'Open snippet drawer'}
            aria-expanded=${panel._snippetDrawerOpen}
            title="Quick-insert snippets"
          >
            ✂️
          </button>
          <aic-speech-to-text
            @transcript=${(e) => panel._onTranscript(e)}
            @recognition-error=${(e) => panel._onRecognitionError(e)}
          ></aic-speech-to-text>
        </div>
        ${panel._streaming
          ? html`<button
              class="send-button stop"
              @click=${() => cancel(panel)}
              aria-label="Stop streaming"
            >
              ⏹ Stop
            </button>`
          : html`<button
              class="send-button"
              ?disabled=${!panel.rpcConnected ||
              (!panel._input.trim() && panel._pendingImages.length === 0)}
              @click=${() => send(panel)}
              aria-label="Send message"
            >
              Send
            </button>`}
      </div>
    </div>
  `;
}

/** What stands in for the input surface on a subagent transcript. */
function renderReadOnlyNote() {
  return html`
    <div class="read-only-note" role="note">
      Read-only transcript — there is no channel to a subagent. Switch to
      Main to send a message.
    </div>
  `;
}

// ---------------------------------------------------------------
// Pending images + lightbox
// ---------------------------------------------------------------

export function renderPendingImages(panel) {
  return html`
    <div class="pending-images" role="list"
      aria-label="Attached images">
      ${panel._pendingImages.map(
        (dataUri, i) => html`
          <div class="pending-image-wrapper" role="listitem">
            <img
              class="pending-image"
              src=${dataUri}
              alt=""
              @click=${() => openLightbox(panel, dataUri)}
              title="Click to view, × to remove"
            />
            <button
              class="pending-image-remove"
              @click=${() => removePendingImage(panel, i)}
              aria-label="Remove image"
              title="Remove image"
            >
              ×
            </button>
          </div>
        `,
      )}
    </div>
  `;
}

export function renderLightbox(panel) {
  // Inline overlay rather than a separate
  // component — simple enough that the extra
  // file would be overkill. Focus the backdrop so
  // Escape works without the user having to click
  // first.
  return html`
    <div
      class="lightbox-backdrop"
      tabindex="0"
      @click=${(e) => {
        // Close on backdrop click but not on
        // clicks inside the content. Check target
        // === currentTarget so the content's own
        // click doesn't bubble up and dismiss.
        if (e.target === e.currentTarget) closeLightbox(panel);
      }}
      @keydown=${(e) => onLightboxKeyDown(panel, e)}
      aria-modal="true"
      role="dialog"
    >
      <div class="lightbox-content">
        <img
          class="lightbox-image"
          src=${panel._lightboxImage}
          alt=""
        />
        <div class="lightbox-actions">
          <button
            class="lightbox-button"
            @click=${() => {
              reattachImage(panel, panel._lightboxImage);
              closeLightbox(panel);
            }}
            title="Re-attach this image to your message"
          >
            📎 Re-attach
          </button>
          <button
            class="lightbox-button"
            @click=${() => closeLightbox(panel)}
            title="Close (Escape)"
          >
            ✕ Close
          </button>
        </div>
      </div>
    </div>
  `;
}

// ---------------------------------------------------------------
// Message rendering
// ---------------------------------------------------------------

/**
 * Render a single message card. Dispatches by
 * role — user / assistant / system-event — and
 * pulls in the right body renderer for each.
 *
 * Search highlight applies via a class on the
 * outer card; the renderer that drives the
 * highlight is computeSearchMatches in search.js,
 * which walks the messages array each render
 * pass.
 */
export function renderMessage(panel, msg, index) {
  // A compaction boundary is not a message anyone wrote — it is a mark in the
  // transcript saying the agent's memory of everything above it is now a
  // summary. It gets no role label, no toolbar and no body, because there is
  // nothing to attribute, copy or reply to.
  if (msg.compaction) return renderCompactionDivider(panel, msg, index);
  const roleClass = msg.system_event
    ? 'role-system'
    : `role-${msg.role}`;
  const roleLabel = msg.system_event
    ? 'System'
    : msg.role === 'user'
      ? 'You'
      : 'Assistant';
  // Compute whether this message is the current
  // search match. Matches are resolved by index
  // lookup to avoid per-card regex re-evaluation
  // on every render.
  const matches = computeSearchMatches(panel);
  const currentMatchIdx =
    matches.length > 0
      ? matches[Math.max(0, panel._searchCurrentIndex) % matches.length]
      : -1;
  const isHighlighted =
    panel._searchQuery.trim() !== '' &&
    index === currentMatchIdx;
  // User content and system-event content both
  // go through the markdown renderer so lists,
  // paragraphs, code fences, etc. render as
  // intended. Angle brackets survive as the
  // text they were typed as — the renderer
  // escapes raw HTML rather than passing it
  // through, so a prompt about `<your topic>`
  // still says so. Assistant content goes
  // through the edit-block segmenter so edit
  // blocks become visual cards instead of raw
  // prose.
  let bodyHtml;
  if (msg.role === 'user' && !msg.system_event) {
    bodyHtml = html`
      <div class="md-content">
        ${unsafeHTML(renderMarkdown(msg.content))}
      </div>
    `;
  } else if (msg.role === 'assistant' && Array.isArray(msg.blocks)) {
    // Claude Code turn. The blocks ARE the body — text, thinking, tool cards,
    // subagent rows, the plan — in the order the engine produced them.
    bodyHtml = renderTurnBlocks(panel, msg.blocks, {
      subagents: msg.subagents,
      settled: true,
    });
  } else if (msg.role === 'assistant') {
    bodyHtml = renderAssistantBody(
      panel,
      msg.content,
      msg.editResults,
      false,
    );
  } else {
    bodyHtml = html`
      <div class="md-content">
        ${unsafeHTML(renderMarkdown(msg.content))}
      </div>
    `;
  }
  const images = Array.isArray(msg.images) ? msg.images : [];
  // A restored prompt carries pointers where a live one carries bytes. Both
  // draw the same strip, so an image the user pasted an hour ago and one they
  // pasted this turn look the same and re-attach the same.
  const imageRefs = imageRefsOf(msg);
  const toolbar = renderMessageToolbar(panel, msg, index);
  const highlightClass = isHighlighted ? ' search-highlight' : '';
  const isBlockTurn =
    msg.role === 'assistant' && !msg.system_event && Array.isArray(msg.blocks);
  // Split badge placement by severity. A natural finish is positive
  // confirmation that the turn ended cleanly — it belongs at the end of the
  // response, where the eye lands when finished reading. Everything else
  // (interrupted, refused, limit reached, engine fault) stays next to the role
  // label at the top so users notice it before reading.
  //
  // Two vocabularies coexist during the engine conversion: the native path's
  // provider `finishReason`, and Claude Code's `terminal_reason`. They are
  // placed by the same rule but labelled by different tables, because a
  // `terminal_reason` of `completed` and a `finishReason` of `stop` are not the
  // same claim and one badge cannot honestly cover both.
  const terminal = isBlockTurn ? terminalBadge(msg.terminalReason) : null;
  const isNaturalFinish = isBlockTurn
    ? terminal?.placement === 'footer'
    : msg.role === 'assistant'
      && !msg.system_event
      && (msg.finishReason === 'stop' || msg.finishReason === 'end_turn');
  const topFinishBadge = isBlockTurn
    ? (terminal && terminal.placement === 'header'
        ? renderTerminalBadge(msg.terminalReason)
        : '')
    : msg.finishReason && !isNaturalFinish
      ? renderFinishBadge(msg.finishReason)
      : '';
  // Frozen run-timer badge — assistant messages only,
  // and only when a duration was recorded at completion.
  // Sits next to the role label, mirroring the live timer
  // on the streaming card so the number lands in the same
  // spot before and after the stream settles.
  const runTimerBadge =
    msg.role === 'assistant' &&
    !msg.system_event &&
    typeof msg.durationMs === 'number'
      ? renderRunTimer(msg.durationMs, false)
      : '';
  const bottomFinishBadge = isNaturalFinish
    ? (isBlockTurn
        ? renderTerminalBadge(msg.terminalReason)
        : renderFinishBadge(msg.finishReason))
    : '';
  // Summary below the body. A Claude Code turn gets one footer carrying files
  // modified, tool-call and prompt counts, duration, per-model usage and cost.
  // The native path keeps its two-banner arrangement — an edit summary derived
  // from our own apply pipeline, and a file summary derived by scanning the
  // prose for paths. Neither has an equivalent here: the agent owns its edits
  // and reports the files it touched, so the footer states what happened
  // instead of inferring it.
  const fileSummary = isBlockTurn
    ? renderTurnFooter(panel, msg.turn, msg.files)
    : msg.role === 'assistant' && !msg.system_event
      ? renderFileSummary(panel, collectMessageFiles(panel, msg))
      : '';
  const editSummary =
    msg.role === 'assistant' && !msg.system_event && !isBlockTurn
      ? renderEditSummary(panel, msg)
      : '';
  // "View subagents (N)" for a turn that delegated. Only Claude Code turns
  // have subagents to read — they are the agent's own `Task` calls — and
  // only a turn whose rows are no longer in the strip needs the affordance,
  // which `renderViewSubagentsAffordance` decides.
  const viewSubagents = isBlockTurn
    ? renderViewSubagentsAffordance(panel, msg)
    : '';
  const finishFooterClass = bottomFinishBadge
    ? ' has-finish-footer'
    : '';
  return html`
    <div
      class="message-card ${roleClass}${highlightClass}${finishFooterClass}"
      data-msg-index=${index}
    >
      <div class="message-toolbar top">${toolbar}</div>
      <div class="role-label">${roleLabel}${topFinishBadge}${runTimerBadge}</div>
      ${bodyHtml}
      ${images.length > 0 || imageRefs.length > 0
        ? renderMessageImages(panel, images, imageRefs)
        : ''}
      ${editSummary}
      ${fileSummary}
      ${viewSubagents}
      ${bottomFinishBadge
        ? html`<div class="finish-reason-footer">${bottomFinishBadge}</div>`
        : ''}
      <div class="message-toolbar bottom">${toolbar}</div>
    </div>
  `;
}

/**
 * Render a compaction boundary as a divider.
 *
 * Kept deliberately quiet: a rule, a label, the before/after counts, and the
 * trigger when the engine named one. The sentence explaining what compaction
 * means to the conversation lives in the tooltip rather than the transcript,
 * because a user who sees three of these in a session should not have to read
 * the same paragraph three times.
 *
 * Still a `.message-card` with a `data-msg-index`, so chat search can scroll to
 * it and highlight it like any other entry — the boundary's token counts are
 * exactly the sort of thing someone searches back for.
 *
 * Per specs5/5-webapp/chat.md § Engine Event Routing.
 */
export function renderCompactionDivider(panel, msg, index) {
  const { counts, trigger } = compactionSummary(msg.compaction);
  const matches = computeSearchMatches(panel);
  const currentMatchIdx =
    matches.length > 0
      ? matches[Math.max(0, panel._searchCurrentIndex) % matches.length]
      : -1;
  const isHighlighted =
    panel._searchQuery.trim() !== '' && index === currentMatchIdx;
  return html`
    <div
      class="message-card compaction-divider${isHighlighted
        ? ' search-highlight'
        : ''}"
      data-msg-index=${index}
      title="The agent's memory of everything above this line is now a summary. This transcript is unchanged."
    >
      <span class="compaction-rule"></span>
      <span class="compaction-label">
        <span class="compaction-glyph" aria-hidden="true">⌁</span>
        Context compacted
        ${counts
          ? html`<span class="compaction-counts">${counts}</span>`
          : ''}
        ${trigger
          ? html`<span class="compaction-trigger">${trigger}</span>`
          : ''}
      </span>
      <span class="compaction-rule"></span>
    </div>
  `;
}

/**
 * Render the finish-reason badge for a message.
 *
 * Severity rules (matches
 * specs-reference/5-webapp/chat.md §
 * Finish-reason badge labels):
 *
 *   - `stop` / `end_turn` → muted green ✓ badge
 *   - `length` / `content_filter` → red badge
 *   - `tool_calls` / `function_call` → amber
 *   - anything else → amber, with the raw reason
 *     surfaced verbatim so unexpected provider
 *     values stay diagnosable
 */
export function renderFinishBadge(reason) {
  let icon = '⚠️';
  let label = reason;
  let severity = '';
  switch (reason) {
    case 'stop':
      icon = '✓';
      label = 'stopped';
      severity = 'severity-natural';
      break;
    case 'end_turn':
      icon = '✓';
      label = 'end of turn';
      severity = 'severity-natural';
      break;
    case 'length':
      icon = '✂️';
      label = 'truncated (max_tokens)';
      severity = 'severity-error';
      break;
    case 'content_filter':
      icon = '🚫';
      label = 'content filter';
      severity = 'severity-error';
      break;
    case 'tool_calls':
      icon = '🔧';
      label = 'tool call requested';
      break;
    case 'function_call':
      icon = '🔧';
      label = 'function call requested';
      break;
    default:
      icon = '⚠️';
      label = reason;
      break;
  }
  const classes = severity
    ? `finish-reason-badge ${severity}`
    : 'finish-reason-badge';
  return html`<span
    class=${classes}
    title="LLM finish reason: ${reason}"
  >${icon} ${label}</span>`;
}

/**
 * Render the run-timer badge — how long the assistant ran
 * for this turn.
 *
 * Two modes, selected by `live`:
 *   - live (streaming card): elapsed = now - startedAt,
 *     recomputed every render. The 250ms panel ticker
 *     (startStreamTimerTick) drives the re-render so the
 *     counter visibly advances. Returns '' when there's no
 *     start stamp (defensive — the streaming card only
 *     renders while a stream is in flight, which always
 *     stamps).
 *   - settled (assistant message card): `ms` is the frozen
 *     duration baked onto the message at completion. Returns
 *     '' when the message carries no duration (older records,
 *     adopted collaborator streams).
 *
 * The ⏱ glyph plus a title make the number self-explanatory
 * without a separate label.
 */
export function renderRunTimer(ms, live) {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms < 0) {
    return '';
  }
  const text = formatRunDuration(ms);
  const cls = live ? 'run-timer run-timer-live' : 'run-timer';
  const title = live
    ? `Assistant running — ${text} elapsed`
    : `Assistant ran for ${text}`;
  return html`<span class=${cls} title=${title}>⏱ ${text}</span>`;
}

/**
 * Render the action toolbar for a message — copy
 * raw text, paste raw text into the chat input,
 * and (when the browser supports speech synthesis)
 * read the message aloud. Used at both top-right
 * and bottom-right of each message card.
 *
 * Returns the same fragment for both placements
 * — Lit deduplicates the underlying event
 * bindings.
 *
 * `index` is the message's position in the
 * messages array; it drives the speaker button's
 * play/stop toggle via `panel._speakingMsgIndex`.
 */
export function renderMessageToolbar(panel, msg, index) {
  const speaking = panel._speakingMsgIndex === index;
  return html`
    <button
      class="message-action-button"
      title="Copy raw text"
      aria-label="Copy message text to clipboard"
      @click=${(e) => {
        // Prevent click-through to the card /
        // mention handler.
        e.stopPropagation();
        copyMessageText(panel, msg);
      }}
    >
      📋
    </button>
    <button
      class="message-action-button"
      title="Paste into input"
      aria-label="Insert message text into chat input"
      @click=${(e) => {
        e.stopPropagation();
        pasteMessageToPrompt(panel, msg);
      }}
    >
      ↩
    </button>
    ${isSpeechSynthesisSupported()
      ? html`<button
          class="message-action-button ${speaking ? 'speaking' : ''}"
          title=${speaking
            ? 'Stop reading'
            : 'Read aloud (or select text first to read just that)'}
          aria-label=${speaking
            ? 'Stop reading message aloud'
            : 'Read message aloud'}
          aria-pressed=${speaking}
          @click=${(e) => {
            e.stopPropagation();
            const card = e.target.closest('.message-card');
            speakMessage(panel, msg, index, card);
          }}
        >
          ${speaking ? '⏹' : '🔊'}
        </button>`
      : ''}
  `;
}

/**
 * Render the edit summary banner for an
 * assistant message. Appears at the end of the
 * message (after all edit cards) when the
 * response contained at least one edit.
 *
 * Per specs-reference/5-webapp/chat.md §Edit
 * Summary:
 *
 *   - Aggregate counts as color-coded stat badges
 *   - Individual failure listing when failures
 *     are present
 *   - Note about a populated retry prompt when
 *     applicable
 *
 * Returns empty when there are no edit results,
 * or when results array is all zero counts.
 */
export function renderEditSummary(panel, msg) {
  if (!msg || msg.role !== 'assistant') return '';
  const results = Array.isArray(msg.editResults)
    ? msg.editResults
    : [];
  if (results.length === 0) return '';

  // Aggregate by status. Count `applied` and
  // `already_applied` separately so the badge
  // text matches what the user actually got
  // (idempotent re-applies shouldn't look like
  // fresh writes).
  let applied = 0;
  let alreadyApplied = 0;
  let failed = 0;
  let skipped = 0;
  let notInContext = 0;
  const failures = [];
  for (const r of results) {
    if (!r) continue;
    const status = r.status;
    if (status === 'applied') applied += 1;
    else if (status === 'already_applied') alreadyApplied += 1;
    else if (status === 'failed') {
      failed += 1;
      failures.push(r);
    } else if (status === 'skipped') {
      skipped += 1;
      failures.push(r);
    } else if (status === 'not_in_context') {
      notInContext += 1;
      failures.push(r);
    }
  }

  // Detect whether a retry prompt was populated.
  // We don't track this as state; re-derive from
  // the same conditions the builder uses.
  //
  // A third condition sat here: an `anchor_not_found`
  // on a file that was in the picker's selection, which
  // meant "the model was shown this file and still
  // missed the anchor". There is no selection to ask
  // (``specs5/plan/decisions.md`` CC-21), and no
  // browser-side set that answers the same question —
  // what the model was shown is the CLI's business now.
  const hasAmbiguous = results.some(
    (r) => r && r.error_type === 'ambiguous_anchor',
  );
  const hasNotInContext = notInContext > 0;
  const retryPromptPopulated = hasAmbiguous || hasNotInContext;

  const stats = [];
  if (applied > 0) {
    stats.push(html`
      <span class="edit-summary-stat applied">
        ✅ ${applied} applied
      </span>
    `);
  }
  if (alreadyApplied > 0) {
    stats.push(html`
      <span class="edit-summary-stat applied">
        ✅ ${alreadyApplied} already applied
      </span>
    `);
  }
  if (failed > 0) {
    stats.push(html`
      <span class="edit-summary-stat failed">
        ❌ ${failed} failed
      </span>
    `);
  }
  if (skipped > 0) {
    stats.push(html`
      <span class="edit-summary-stat skipped">
        ⚠️ ${skipped} skipped
      </span>
    `);
  }
  if (notInContext > 0) {
    stats.push(html`
      <span class="edit-summary-stat not-in-context">
        ⚠️ ${notInContext} not in context
      </span>
    `);
  }
  if (stats.length === 0) return '';

  return html`
    <div class="edit-summary" role="status">
      <div class="edit-summary-header">
        <span class="edit-summary-title">Edits:</span>
        ${stats}
      </div>
      ${failures.length > 0
        ? html`
            <div class="edit-summary-failures">
              ${failures.map((r) => {
                const path = r.file_path || r.file || '(unknown)';
                const errorType =
                  typeof r.error_type === 'string' ? r.error_type : '';
                const message =
                  typeof r.message === 'string' ? r.message : '';
                return html`
                  <div class="edit-summary-failure">
                    <span
                      class="edit-summary-failure-path"
                      @click=${() => {
                        window.dispatchEvent(
                          new CustomEvent('navigate-file', {
                            detail: { path },
                            bubbles: false,
                          }),
                        );
                      }}
                      title="Open ${path}"
                    >${path}</span>
                    ${errorType
                      ? html`<span
                          class="edit-summary-failure-type"
                        >${errorType}</span>`
                      : ''}
                    ${message
                      ? html`<span
                          class="edit-summary-failure-message"
                        >${message}</span>`
                      : ''}
                  </div>
                `;
              })}
            </div>
          `
        : ''}
      ${retryPromptPopulated
        ? html`
            <div class="edit-summary-retry-note">
              A retry prompt has been prepared in the input
              below.
            </div>
          `
        : ''}
    </div>
  `;
}

/**
 * The image strip under a message: the bytes it already holds, then whatever
 * its pointers have resolved to.
 *
 * `images` is data URIs — what a live paste puts on the message. `refs` is
 * pointers into the transcript, which is what a restored one carries
 * (`specs5/4-features/images.md` § Reading Flow); they resolve to bytes
 * asynchronously through `panel._imageRefData`, and until they do the tile is
 * drawn at its final size so the transcript does not reflow image by image as
 * they land.
 *
 * A resolved pointer becomes the same tile a pasted image gets, lightbox and
 * re-attach included. That is the spec's requirement rather than a
 * convenience: re-attaching from a past session is one of the two documented
 * paths into the composer, and it works by sending a fresh copy of the bytes.
 */
export function renderMessageImages(panel, images, refs = []) {
  return html`
    <div class="message-images" role="list">
      ${images.map((dataUri) => renderImageTile(panel, dataUri))}
      ${refs.map((ref) => {
        const entry = panel._imageRefData?.get(imageRefKey(ref));
        if (entry?.dataUri) return renderImageTile(panel, entry.dataUri);
        // A pointer that will never resolve keeps a tile of its own with the
        // reason. An image silently absent from a prompt reads as a prompt
        // that never had one, which is a different conversation from the one
        // that happened.
        if (entry?.error) {
          return html`
            <div
              class="message-image-wrapper message-image-missing"
              role="listitem"
              title=${entry.error}
            >
              🚫
            </div>
          `;
        }
        return html`
          <div
            class="message-image-wrapper message-image-pending"
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

function renderImageTile(panel, dataUri) {
  return html`
    <div class="message-image-wrapper" role="listitem">
      <img
        class="message-image"
        src=${dataUri}
        alt=""
        @click=${() => openLightbox(panel, dataUri)}
        title="Click to view"
      />
      <button
        class="message-image-reattach"
        @click=${(e) => {
          // Don't also open the lightbox
          // from the click-through on the
          // image itself.
          e.stopPropagation();
          reattachImage(panel, dataUri);
        }}
        aria-label="Re-attach image to your message"
        title="Re-attach to composition"
      >
        📎
      </button>
    </div>
  `;
}

/**
 * Render assistant message body as a mix of
 * prose segments (through markdown) and edit-
 * block segments (through the renderer). The
 * parser handles code-fence stripping around
 * edit blocks; prose segments are passed to
 * marked as-is.
 *
 * `editResults` from the backend's stream-
 * complete payload pairs each edit segment with
 * its applied/failed/skipped/not_in_context
 * result via matchSegmentsToResults.
 *
 * File mention detection runs on prose segments
 * ONLY when `isStreaming` is false. Mid-stream
 * content grows chunk by chunk; running mention
 * detection on partial prose could wrap a path
 * just as the LLM is about to extend it into a
 * different word (`src/foo.py` becomes
 * `src/foo.pyc` mid-stream).
 */
export function renderAssistantBody(panel, content, editResults, isStreaming) {
  const segments = segmentResponse(content || '');
  if (segments.length === 0) {
    return html`<div class="md-content"></div>`;
  }
  const matched = matchSegmentsToResults(
    segments,
    Array.isArray(editResults) ? editResults : [],
  );
  const wrapMentions =
    !isStreaming &&
    Array.isArray(panel.repoFiles) &&
    panel.repoFiles.length > 0;
  const parts = segments.map((seg, i) => {
    if (seg.type === 'text') {
      let html_ = renderMarkdown(seg.content);
      if (wrapMentions) {
        html_ = findFileMentions(html_, panel.repoFiles);
      }
      return html`
        <div class="md-content">${unsafeHTML(html_)}</div>
      `;
    }
    // edit and edit-pending both go through
    // renderEditCard. Pending segments resolve
    // to the 'pending' status badge; completed
    // segments use their matched result.
    const cardHtml = renderEditCard(seg, matched[i] || null);
    return html`${unsafeHTML(cardHtml)}`;
  });
  return html`<div class="assistant-body">${parts}</div>`;
}

/**
 * Render the streaming card — the live turn, using the assistant-role styling
 * with an accent border to distinguish it from settled messages.
 *
 * The body is the same block renderer the settled card uses, with
 * `settled: false`. That is the point: a tool card mid-turn and the same card
 * after the turn ends are the same element in the same place, so nothing jumps
 * when the result lands. What `settled: false` changes is only what would be
 * wrong to claim early — file mentions wait for final content, and the plan is
 * marked live.
 *
 * A turn with no blocks yet gets a waiting line instead of an empty card. The
 * gap between "request accepted" and the first chunk is real (the engine spawns
 * the CLI, the CLI initialises) and an empty card reads as a hang.
 *
 * The blinking cursor sits after the body so it is visible whatever the last
 * block is, and the live token counter sits after the cursor: it is the
 * streaming card's footer, and the same chips land in the same place when the
 * settled card's real footer replaces them.
 */
export function renderStreamingMessage(panel) {
  // Live run timer — elapsed since the prompt was sent.
  // `_streamStartedAt` is stamped on the active tab in
  // send() (and resume / agent-spawn paths); the 250ms
  // panel ticker re-renders this card so the counter
  // advances. Missing stamp → no timer (defensive).
  const startedAt = panel._streamStartedAt;
  const elapsedMs =
    typeof startedAt === 'number'
      ? Math.max(0, Date.now() - startedAt)
      : null;
  const turn = panel._turnBlocks;
  return html`
    <div class="message-card role-assistant streaming">
      <div class="role-label">
        Assistant${elapsedMs != null
          ? renderRunTimer(elapsedMs, true)
          : ''}
      </div>
      ${isEmptyTurn(turn)
        ? html`<div class="turn-waiting">Working…</div>`
        : renderTurnBlocks(panel, turn.blocks, {
            subagents: turn.subagents,
            settled: false,
          })}
      <span class="cursor"></span>
      ${renderLiveUsage(turn?.usage)}
    </div>
  `;
}

// `renderRetryBanner` stood here until conversion phase 3, drawing a
// countdown while AIC⚡DC's completion wrapper slept between attempts. The CLI
// retries inside the subprocess without narrating it, so there is no wait to
// count down. Rate limits — the one retryable condition it does report —
// arrive as a `rateLimit` message carrying a real reset time, handled in
// streaming.js.

// ---------------------------------------------------------------
// Search bar
// ---------------------------------------------------------------

/**
 * Render the search bar — input box with mode
 * toggle, three search-option toggles
 * (ignoreCase, regex, wholeWord), match counter,
 * and navigation arrows. The mode + cross-ref
 * toggle group that sat at the right went in
 * conversion phases 2 and 4.
 *
 * Counter and nav handlers are mode-dependent:
 * file mode shows "N in F files" + uses the
 * file-search nav handlers; message mode shows
 * "current/total" + uses the message-search nav
 * handlers.
 */
export function renderSearchBar(panel) {
  const fileMode = panel._searchMode === 'file';
  const hasQuery = panel._searchQuery.trim().length > 0;
  let counterText = '';
  let noMatch = false;
  let navTotal = 0;
  if (fileMode) {
    const matchCount = totalFileSearchMatches(panel);
    const fileCount = panel._fileSearchResults.length;
    navTotal = matchCount;
    if (panel._fileSearchLoading) {
      counterText = 'Searching…';
    } else if (hasQuery) {
      counterText =
        matchCount === 0
          ? '0 results'
          : `${matchCount} in ${fileCount}`;
      noMatch = matchCount === 0;
    }
  } else {
    const matches = computeSearchMatches(panel);
    const total = matches.length;
    navTotal = total;
    if (hasQuery) {
      const current =
        total === 0
          ? 0
          : Math.min(
              Math.max(0, panel._searchCurrentIndex) + 1,
              total,
            );
      counterText = `${current}/${total}`;
      noMatch = total === 0;
    }
  }
  const placeholder = fileMode
    ? 'Search files…'
    : 'Search messages…';
  const ariaLabel = fileMode
    ? 'Search repository files'
    : 'Search messages';
  const onPrev = fileMode
    ? () => onFileSearchPrev(panel)
    : () => onSearchPrev(panel);
  const onNext = fileMode
    ? () => onFileSearchNext(panel)
    : () => onSearchNext(panel);
  // The mode toggle is a single button that flips
  // between message and file search. Its glyph shows
  // the mode you'd switch INTO when in message mode
  // (so 💬 actually means "currently in message
  // mode"); when active (file mode) it switches to
  // 📁 and gets the .active class for visual
  // feedback.
  const toggleTitle = fileMode
    ? 'Switch to message search'
    : 'Switch to file search';
  return html`
    <div class="search-bar" role="search">
      <button
        type="button"
        class="search-mode-toggle ${fileMode ? 'active' : ''}"
        @click=${() => toggleSearchMode(panel)}
        aria-pressed=${fileMode}
        title=${toggleTitle}
      >${fileMode ? '📁' : '💬'}</button>
      <div class="search-input-wrapper">
        <input
          type="text"
          class="search-input"
          placeholder=${placeholder}
          .value=${panel._searchQuery}
          @input=${(e) => onSearchInput(panel, e)}
          @keydown=${(e) => onSearchKeyDown(panel, e)}
          aria-label=${ariaLabel}
        />
        <button
          class="search-toggle ${panel._searchIgnoreCase
            ? 'active'
            : ''}"
          @click=${() => toggleSearchOption(panel, 'ignoreCase')}
          aria-pressed=${panel._searchIgnoreCase}
          title="Ignore case"
        >
          Aa
        </button>
        <button
          class="search-toggle ${panel._searchRegex
            ? 'active'
            : ''}"
          @click=${() => toggleSearchOption(panel, 'regex')}
          aria-pressed=${panel._searchRegex}
          title="Regex"
        >
          .*
        </button>
        <button
          class="search-toggle ${panel._searchWholeWord
            ? 'active'
            : ''}"
          @click=${() => toggleSearchOption(panel, 'wholeWord')}
          aria-pressed=${panel._searchWholeWord}
          title="Whole word"
        >
          ab
        </button>
      </div>
      <span
        class="search-counter ${noMatch ? 'no-match' : ''}"
        aria-live="polite"
      >
        ${counterText}
      </span>
      <div class="search-nav" aria-label="Match navigation">
        <button
          class="search-nav-button"
          ?disabled=${navTotal === 0}
          @click=${onPrev}
          aria-label="Previous match"
          title="Previous (Shift+Enter)"
        >
          ▲
        </button>
        <button
          class="search-nav-button"
          ?disabled=${navTotal === 0}
          @click=${onNext}
          aria-label="Next match"
          title="Next (Enter / ↓)"
        >
          ▼
        </button>
      </div>
    </div>
  `;
}

// ---------------------------------------------------------------
// Snippet drawer
// ---------------------------------------------------------------

export function renderSnippetDrawer(panel) {
  // Empty list (pre-load, post-error, or
  // genuinely no snippets configured) shows a
  // placeholder rather than an empty box. Opening
  // the drawer is a deliberate action so showing
  // nothing would be confusing.
  if (panel._snippets.length === 0) {
    return html`
      <div class="snippet-drawer" role="region"
        aria-label="Snippet drawer">
        <div class="snippet-empty">No snippets available</div>
      </div>
    `;
  }
  return html`
    <div
      class="snippet-drawer"
      role="region"
      aria-label="Snippet drawer"
    >
      ${panel._snippets.map(
        (snippet) => html`
          <button
            class="snippet-button"
            title=${snippet.tooltip || snippet.message || ''}
            aria-label=${snippet.tooltip ||
            `Insert snippet: ${snippet.message || ''}`}
            @click=${() => insertSnippet(panel, snippet)}
          >
            <span class="snippet-icon">${snippet.icon || '✂'}</span>
            ${snippet.tooltip
              ? html`<span class="snippet-label"
                  >${snippet.tooltip}</span
                >`
              : ''}
          </button>
        `,
      )}
    </div>
  `;
}

// ---------------------------------------------------------------
// File search overlay
// ---------------------------------------------------------------

export function renderFileSearchOverlay(panel) {
  if (!panel._searchQuery.trim()) {
    return html`
      <div class="file-search-overlay">
        <div class="file-search-empty">
          Type to search across files
        </div>
      </div>
    `;
  }
  if (panel._fileSearchLoading && panel._fileSearchResults.length === 0) {
    return html`
      <div class="file-search-overlay">
        <div class="file-search-empty">Searching…</div>
      </div>
    `;
  }
  if (panel._fileSearchResults.length === 0) {
    return html`
      <div class="file-search-overlay">
        <div class="file-search-empty">No results found</div>
      </div>
    `;
  }
  // Walk results, maintaining a running flat-
  // index so each match row carries its position
  // in the overall navigation sequence. The
  // focused row gets `.focused` class and a
  // `data-file-match-flat` attribute so scroll-
  // sync can target it.
  let flatIndex = 0;
  const sections = [];
  for (let fi = 0; fi < panel._fileSearchResults.length; fi += 1) {
    const entry = panel._fileSearchResults[fi];
    const matches = Array.isArray(entry?.matches) ? entry.matches : [];
    const matchRows = matches.map((match) => {
      const thisFlat = flatIndex++;
      return renderFileSearchMatch(
        panel,
        entry.file,
        match,
        thisFlat,
      );
    });
    sections.push(html`
      <div
        class="file-search-section"
        data-file-section=${entry.file}
      >
        <div
          class="file-section-header"
          @click=${() => onFileSearchHeaderClick(entry.file)}
          title="Open ${entry.file}"
        >
          <span class="file-section-path">${entry.file}</span>
          <span class="file-section-count">
            ${matches.length}
          </span>
        </div>
        ${matchRows}
      </div>
    `);
  }
  return html`
    <div
      class="file-search-overlay"
      @scroll=${(e) => onFileSearchOverlayScroll(panel, e)}
    >
      ${sections}
    </div>
  `;
}

/**
 * Render a single match row — context lines
 * before, the match line itself, context lines
 * after. Context rows are not clickable and not
 * focusable; only the match line navigates.
 */
function renderFileSearchMatch(panel, filePath, match, flatIndex) {
  if (!match) return '';
  const isFocused = flatIndex === panel._fileSearchFocusedIndex;
  const before = Array.isArray(match.context_before)
    ? match.context_before
    : [];
  const after = Array.isArray(match.context_after)
    ? match.context_after
    : [];
  return html`
    ${before.map(
      (ctx) => html`
        <div class="file-match-row context">
          <span class="file-match-linenum">
            ${ctx.line_num ?? ''}
          </span>
          <span class="file-match-text">${ctx.line ?? ''}</span>
        </div>
      `,
    )}
    <div
      class="file-match-row ${isFocused ? 'focused' : ''}"
      data-file-match-flat=${flatIndex}
      @click=${() => onFileSearchMatchClick(filePath, match)}
    >
      <span class="file-match-linenum">
        ${match.line_num ?? ''}
      </span>
      <span class="file-match-text">
        ${renderHighlightedMatchLine(panel, match.line ?? '')}
      </span>
    </div>
    ${after.map(
      (ctx) => html`
        <div class="file-match-row context">
          <span class="file-match-linenum">
            ${ctx.line_num ?? ''}
          </span>
          <span class="file-match-text">${ctx.line ?? ''}</span>
        </div>
      `,
    )}
  `;
}

/**
 * Highlight occurrences of the search query
 * within a match line. Regex / whole-word /
 * ignore-case toggles are respected. Falls back
 * to the plain line when the pattern can't be
 * built (invalid regex).
 */
function renderHighlightedMatchLine(panel, line) {
  if (!line) return '';
  const query = panel._searchQuery;
  if (!query.trim()) return line;
  let pattern;
  try {
    let source = panel._searchRegex
      ? query
      : query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    if (panel._searchWholeWord) {
      source = `\\b(?:${source})\\b`;
    }
    const flags = panel._searchIgnoreCase ? 'gi' : 'g';
    pattern = new RegExp(source, flags);
  } catch (_) {
    return line;
  }
  const parts = [];
  let cursor = 0;
  let m;
  pattern.lastIndex = 0;
  while ((m = pattern.exec(line)) !== null) {
    if (m.index > cursor) {
      parts.push(line.slice(cursor, m.index));
    }
    parts.push(
      html`<span class="file-match-highlight"
        >${m[0]}</span
      >`,
    );
    cursor = m.index + m[0].length;
    // Guard against zero-width matches —
    // infinite loop prevention.
    if (m[0].length === 0) {
      pattern.lastIndex += 1;
    }
  }
  if (cursor < line.length) {
    parts.push(line.slice(cursor));
  }
  return parts;
}

function onFileSearchMatchClick(filePath, match) {
  window.dispatchEvent(
    new CustomEvent('navigate-file', {
      detail: { path: filePath, line: match?.line_num },
      bubbles: false,
    }),
  );
}

function onFileSearchHeaderClick(filePath) {
  window.dispatchEvent(
    new CustomEvent('navigate-file', {
      detail: { path: filePath },
      bubbles: false,
    }),
  );
}

// ---------------------------------------------------------------
// File summary section
// ---------------------------------------------------------------

/**
 * Collect every file path referenced by an
 * assistant message — both edit-block headers
 * and inline prose mentions. Returns `[{path}]`
 * deduplicated in first-seen order.
 *
 * Each entry used to carry an `inContext` flag
 * saying whether the path was in the picker's
 * selection, which drove the chips' ✓/+ marks
 * and the "Add All" button. Both went with the
 * selection (``specs5/plan/decisions.md``
 * CC-21); the summary is a list of what this
 * message named, and every entry is alike.
 *
 * Edit blocks always contribute their
 * `filePath` — the LLM unambiguously named the
 * file as an edit target, so it belongs in the
 * summary regardless of whether the file exists
 * in `repoFiles`.
 *
 * Prose mentions are harvested only when
 * `repoFiles` is non-empty, using the same
 * longest-first substring matching as
 * `findFileMentions`.
 */
export function collectMessageFiles(panel, msg) {
  if (!msg || msg.role !== 'assistant') return [];
  const content =
    typeof msg.content === 'string' ? msg.content : '';
  const seen = new Set();
  const out = [];

  // Edit block file paths.
  if (content) {
    const segments = segmentResponse(content);
    for (const seg of segments) {
      if (
        (seg.type === 'edit' || seg.type === 'edit-pending') &&
        typeof seg.filePath === 'string' &&
        seg.filePath &&
        !seen.has(seg.filePath)
      ) {
        seen.add(seg.filePath);
        out.push({ path: seg.filePath });
      }
    }
  }

  // Inline prose mentions.
  if (
    content &&
    Array.isArray(panel.repoFiles) &&
    panel.repoFiles.length > 0
  ) {
    const candidates = panel.repoFiles.filter(
      (p) => typeof p === 'string' && p && content.includes(p),
    );
    if (candidates.length > 0) {
      candidates.sort((a, b) => {
        if (b.length !== a.length) return b.length - a.length;
        return a.localeCompare(b);
      });
      for (const path of candidates) {
        if (seen.has(path)) continue;
        if (proseContainsPath(content, path)) {
          seen.add(path);
          out.push({ path });
        }
      }
    }
  }

  return out;
}

/**
 * Check whether `path` appears in `content` as a
 * real mention — same boundary rules as the HTML
 * mention matcher. Skips anything inside ```
 * fences. Inline code (single backticks) isn't
 * excluded — per spec, matches inside inline
 * code are wrapped normally.
 */
function proseContainsPath(content, path) {
  let inFence = false;
  const lines = content.split('\n');
  for (const line of lines) {
    if (/^\s*```/.test(line)) {
      inFence = !inFence;
      continue;
    }
    if (inFence) continue;
    if (lineContainsPath(line, path)) return true;
  }
  return false;
}

/**
 * Boundary-aware substring check for a single
 * line. Path characters (letters, digits,
 * underscore, hyphen, slash) never terminate a
 * match; dot is a boundary only at the trailing
 * edge; everything else (whitespace,
 * punctuation) is a boundary on both sides.
 */
function lineContainsPath(line, path) {
  let from = 0;
  while (from <= line.length - path.length) {
    const idx = line.indexOf(path, from);
    if (idx === -1) return false;
    const endIdx = idx + path.length;
    const before = idx > 0 ? line[idx - 1] : '';
    const after = endIdx < line.length ? line[endIdx] : '';
    if (
      isMentionBoundary(before, 'before') &&
      isMentionBoundary(after, 'after')
    ) {
      return true;
    }
    from = idx + 1;
  }
  return false;
}

function isMentionBoundary(ch, position) {
  if (ch === '') return true;
  if (/[A-Za-z0-9_\-/]/.test(ch)) return false;
  if (ch === '.') return position === 'after';
  return true;
}

/**
 * Render the file summary section for an
 * assistant message. Emits nothing when the file
 * list is empty.
 *
 * Layout:
 *
 *   📁 Files Referenced
 *   [↗ path/to/a.py]  [↗ path/to/b.py]
 *
 * Clicking a chip dispatches `file-chip-click`,
 * which opens the file in the viewer.
 *
 * The header carried a "+ Add All (N)" button and
 * each chip a ✓/+ mark showing whether the file
 * was in the picker's selection. Both went with
 * the selection (``specs5/plan/decisions.md``
 * CC-21). What's left is the part that was always
 * useful on its own: the files this message
 * named, collected in one place, one click from
 * open.
 */
export function renderFileSummary(panel, files) {
  if (!Array.isArray(files) || files.length === 0) return '';
  return html`
    <div class="file-summary-section" role="group"
      aria-label="Files referenced by this message">
      <div class="file-summary-header">
        <span class="file-summary-title">
          📁 Files Referenced
        </span>
      </div>
      <div class="file-summary-chips">
        ${files.map(
          (file) => html`
            <button
              class="file-chip"
              @click=${(e) => {
                e.stopPropagation();
                onFileChipClick(panel, file.path);
              }}
              title="${file.path} — click to open"
              aria-label="Open ${file.path}"
            >
              <span class="file-chip-mark" aria-hidden="true">↗</span>
              <span class="file-chip-path">${file.path}</span>
            </button>
          `,
        )}
      </div>
    </div>
  `;
}

/**
 * Render the "View subagents (N)" affordance beneath a settled turn.
 *
 * Per specs5/5-webapp/subagent-browser.md § Historical Transcripts: a
 * previous turn's subagent tabs have left the strip, but their transcripts
 * are still on disk under the session's `subagents/` directory. Scrolling
 * back to the turn surfaces this; clicking it reads each one through
 * `get_subagent_transcript` into a read-only tab.
 *
 * Returns empty when the turn spawned no subagents, when none of its rows
 * names an agent (a row keyed only by task id has nothing to read a
 * transcript by), and when every one of them is still live in the strip —
 * the active turn, where the strip already offers the same thing.
 *
 * A turn read back off disk has no rows at all, because the transcript does
 * not record which turn spawned which subagent. So this appears on turns
 * from the current run, and the history browser is the way into an older
 * session's subagents.
 */
export function renderViewSubagentsAffordance(panel, msg) {
  const rows = Array.isArray(msg?.subagents) ? msg.subagents : [];
  const agents = [];
  for (const row of rows) {
    const agentId = typeof row?.agent_id === 'string' ? row.agent_id : '';
    if (!agentId) continue;
    agents.push({ agent_id: agentId, label: subagentLabel(row) });
  }
  if (agents.length === 0) return '';
  if (agents.every(({ agent_id: id }) => panel._tabs.has(id))) return '';
  const label =
    agents.length === 1
      ? 'View subagent (1)'
      : `View subagents (${agents.length})`;
  return html`
    <div class="view-subagents-affordance">
      <button
        class="view-subagents-button"
        @click=${(e) => {
          e.stopPropagation();
          panel.dispatchEvent(
            new CustomEvent('view-subagents-requested', {
              detail: { agents },
              bubbles: true,
              composed: true,
            }),
          );
        }}
        aria-label=${label}
        title="Read these subagents' transcripts in the tab strip"
      >
        🤖 ${label}
      </button>
    </div>
  `;
}