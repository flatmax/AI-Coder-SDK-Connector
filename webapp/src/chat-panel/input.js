// Input + composition handlers for the ChatPanel.
//
// Owns everything that mediates between the user's
// keystroke / paste / click and the textarea or
// composition state:
//
//   - Textarea input + auto-resize
//   - Enter/Shift+Enter, up-arrow recall, IME guard
//   - Paste handling (text fallthrough, image
//     extraction with size/count caps)
//   - Pending-image strip + lightbox
//   - Speech-to-text transcript insertion
//   - Snippet drawer toggle + insertion
//   - History browser open/close/load
//   - New session
//   - File mention detection (the `@filter` bridge
//     to the picker via `filter-from-chat`)
//   - Slash-command detection (the `/palette` bridge
//     to `ac-slash-palette`, and acting on what it
//     hands back)
//   - Message text extraction for copy/paste
//   - File chip click + Add-All accumulation
//   - Code-block copy-button handling
//   - Mention click delegation
//   - Auto-scroll engagement
//
// Why one module rather than several: these
// handlers all cluster around the textarea + the
// message list. Splitting them further would
// require shared state (the textarea ref, the
// shadow root) to be threaded through three or
// four files. Keeping them together makes the
// cross-references explicit and keeps the file
// boundaries aligned with what the user perceives
// as one "input area".

import {
  MAX_IMAGE_BYTES,
  MAX_IMAGES_PER_MESSAGE,
  estimateDataUriBytes,
  extractImagesFromClipboard,
} from '../image-utils.js';
import {
  AUTO_SCROLL_DISENGAGE_PX,
  AUTO_SCROLL_TOLERANCE_PX,
  _saveDrawerOpen,
  generateRequestId,
} from './helpers.js';
import { resetTurnBlocks } from './blocks.js';
import {
  handleStreamStartError,
  handleUnsupportedSlash,
  maybeStopStreamTimerTick,
  startStreamTimerTick,
} from './streaming.js';
import { setSearchMode } from './search.js';
import { clearSubagentTabs } from './subagent-tabs.js';
import {
  completionFor,
  detectActiveSlash,
} from '../slash-commands.js';
import { isSpeechSynthesisSupported } from '../speech-synthesis.js';
import { speechPlayer } from '../speech-player.js';

// localStorage key for the in-progress textarea
// draft. Persisted on every input event so a
// browser refresh, tab reload, or accidental
// close doesn't lose unsent text. Cleared on
// send. Global rather than per-repo because the
// draft is short-lived and threading repoName
// into the chat panel isn't currently wired —
// worst case on repo switch the draft surfaces
// in another repo, which the user can clear with
// one keystroke.
//
// Exported so the test suite can clear it in
// afterEach — without that cleanup, persisted
// drafts from earlier tests leak into later
// mounts via `connectedCallback` and corrupt
// assertions on `_input`.
export const _DRAFT_STORAGE_KEY = 'ac-dc.chat.draft';

export function _loadDraft() {
  try {
    return localStorage.getItem(_DRAFT_STORAGE_KEY) || '';
  } catch {
    return '';
  }
}

export function _saveDraft(value) {
  try {
    if (value) {
      localStorage.setItem(_DRAFT_STORAGE_KEY, value);
    } else {
      localStorage.removeItem(_DRAFT_STORAGE_KEY);
    }
  } catch {
    /* localStorage unavailable — silent. */
  }
}

// ---------------------------------------------------------------
// Send + cancel
// ---------------------------------------------------------------

/**
 * Send the composed message + pending images to Claude Code.
 *
 * Drops the snippet drawer on send (auto-close —
 * users want vertical space back during streaming).
 *
 * `ClaudeCodeService.chat_streaming` takes five arguments —
 * `(request_id, message, files, images, viewer)`. What the native
 * engine's eight-argument form carried and this one does not:
 *
 *   - `excluded_urls` — the URL-chip feature is gone (rendering.js).
 *     The agent fetches what it wants with WebFetch.
 *   - `agent_tag` — there is one CLI session and one turn in flight.
 *     Subagent output is attributed by the `Task` call's
 *     `tool_use_id`, not by routing a separate stream (blocks.js).
 *   - `reasoning` / `effort` — the CLI decides when to think.
 *
 * `viewer` is passed null: it wants the file and selection range the
 * user is looking at, which lives in the shell's viewers, not here.
 * Wiring that gesture is phase 6's; sending a wrong answer now would
 * be worse than sending none.
 *
 * A `/command` is not intercepted. The service answers built-ins that
 * have an AC⚡DC equivalent synchronously with `{status: "unsupported"}`
 * and lets custom commands from `.claude/commands/` through — so a
 * mistyped command becomes a system note, never a question the agent
 * tries to answer.
 */
export async function send(panel) {
  const text = panel._input.trim();
  // Either text or at least one image must be
  // present. An image-only message is valid ("look
  // at this") — the LLM receives it as a user
  // message with just the image content.
  if (!text && panel._pendingImages.length === 0) return;
  if (panel._streaming) return;
  if (!panel.rpcConnected) return;
  // Read-only tab gate. A subagent transcript has no channel to reply
  // down — the subagent finished when its `Task` call returned, and even
  // while it ran the only way in was the agent's own prompt. `render`
  // leaves out the input surface entirely on such a tab, so this is the
  // belt-and-braces guard for a send arriving some other way (a shortcut,
  // a tab switch racing a keystroke). Toast and bail, keeping the text so
  // the user can carry it to Main.
  const activeTab = panel._tabs.get(panel._activeTabId);
  if (activeTab && activeTab.readOnly) {
    panel._emitToast(
      'Read-only transcript — there is no channel to a subagent. Switch to Main to send a message.',
      'warning',
    );
    return;
  }

  // Auto-exit file search mode on send — the user
  // is now composing a message, not scanning
  // results. Matches specs4/5-webapp/search.md.
  if (panel._searchMode === 'file') {
    setSearchMode(panel, 'message');
  }

  const requestId = generateRequestId();
  panel._currentRequestId = requestId;
  panel._streams.set(requestId, { content: '', sticky: true });

  // Snapshot pending images BEFORE we clear the
  // array. The optimistic message shows them; the
  // RPC receives them; the send state clears them
  // regardless of success. Deliberate symmetry:
  // even if the RPC rejects, the user doesn't
  // want their images reappearing in the pending
  // strip.
  const images = panel._pendingImages.slice();

  // Record this message in input history before
  // we clear the textarea — up-arrow recall
  // wants the full text, not the empty string
  // we're about to replace it with. Image-only
  // messages aren't recorded: there's no text
  // to recall and an empty entry would clutter
  // the up-arrow list.
  if (text) {
    const history = panel.shadowRoot?.querySelector(
      'ac-input-history',
    );
    if (history) history.addEntry(text, images);
  }

  // Add the user message optimistically. The
  // server will broadcast `userMessage` shortly;
  // our handler detects the in-flight request and
  // skips the echo.
  const optimistic = {
    role: 'user',
    content: text,
    ...(images.length > 0 ? { images } : {}),
  };
  panel.messages = [...panel.messages, optimistic];
  panel._input = '';
  // The palette is normally already closed by the time Enter
  // reaches here — it consumes Enter whenever it has a match.
  // The case this covers is Enter on a `/typo` with no match,
  // where the overlay is deliberately still up explaining
  // itself and the message goes to the engine anyway.
  panel.shadowRoot?.querySelector('ac-slash-palette')?.hide();
  // Draft has been committed — clear the
  // persisted copy so the next refresh starts
  // clean.
  _saveDraft('');
  panel._pendingImages = [];
  panel._streaming = true;
  panel._streamingContent = '';
  // Clear last turn's blocks before the first chunk of this one can
  // arrive. Without this the streaming card would open showing the
  // previous turn's tool cards and thinking region — content is
  // cumulative within a block and never across a turn (blocks.js), and
  // this is where "across a turn" is enforced on the send side.
  // Reset the tab we're sending from, not the whole panel: a background
  // tab's settled turn is history, not staleness.
  if (activeTab) resetTurnBlocks(activeTab.turnBlocks);
  // Last turn's subagent tabs leave the strip with its blocks, for the same
  // reason: they are feeds of the turn that just ended, and a strip that
  // accumulated every turn's delegations would be the user's to clean up by
  // hand. Their transcripts stay reachable through "View subagents" on the
  // settled turn (specs5/5-webapp/subagent-browser.md § Tab Lifetime). Safe
  // here because the read-only gate above has already established that the
  // tab we are sending from is not one of them.
  clearSubagentTabs(panel);
  // Stamp the run-timer start the instant the prompt is
  // sent, and kick the panel-level ticker so the live
  // elapsed counter on the streaming card starts moving.
  // The stamp is frozen onto the assistant message when
  // the response finishes (onStreamComplete) and cleared
  // there. Stamping on the active tab's accessor writes
  // through to the active tab's state — the same tab the
  // stream is opened against.
  panel._streamStartedAt = Date.now();
  startStreamTimerTick(panel);
  panel._autoScroll = true;
  // Reset the textarea's inline height after
  // clearing. Programmatic value clears don't
  // fire `input`, so the auto-resize logic in
  // `onInputChange` won't run — without this
  // reset, the textarea keeps the height it grew
  // to during composition.
  {
    const ta = panel.shadowRoot?.querySelector('.input-textarea');
    if (ta) ta.style.height = 'auto';
  }
  // Auto-close the snippet drawer on send.
  if (panel._snippetDrawerOpen) {
    panel._snippetDrawerOpen = false;
    _saveDrawerOpen(false);
  }
  try {
    const result = await panel.rpcExtract(
      'ClaudeCodeService.chat_streaming',
      requestId,
      text,
      Array.isArray(panel.selectedFiles)
        ? panel.selectedFiles
        : [],
      images,
      // viewer framing — see the docstring. Explicitly null rather
      // than omitted so the positional arity is unambiguous when a
      // later phase fills it in.
      null,
    );
    // `{status: "started"}` on the happy path. Everything after that
    // arrives as server-push events keyed on `requestId`.
    //
    // Synchronous refusals resolve the Promise with a dict rather than
    // rejecting, so both shapes are checked here. Two of them:
    //
    //   - `{error, reason}` — engine not ready, turn already in
    //     flight, session lost. The turn never started.
    //   - `{status: "routed", command, target, message}` — a command
    //     whose job an AC⚡DC surface does better. Nothing was sent;
    //     the surface opens instead. This is the path for a command
    //     that was *typed* — selecting it in the palette routes
    //     locally and never reaches here.
    //   - `{status: "unsupported", command, message}` — a command that
    //     cannot work in this deployment. Also never started, but it
    //     isn't an error and must not be rendered as one.
    if (result && typeof result === 'object') {
      if (result.error) {
        handleStreamStartError(panel, requestId, result.error);
        return;
      }
      if (result.status === 'routed') {
        rollbackUnstartedTurn(panel, requestId);
        handleRoutedSlash(panel, result);
        return;
      }
      if (result.status === 'unsupported') {
        // Roll the optimistic streaming state back first, then report.
        // Order matters: `handleUnsupportedSlash` appends to
        // `panel.messages`, and the streaming card renders below the
        // list — leaving it up would put a spinner under the note
        // saying nothing was sent.
        rollbackUnstartedTurn(panel, requestId);
        handleUnsupportedSlash(panel, result);
        return;
      }
    }
  } catch (err) {
    console.error('[chat] chat_streaming failed', err);
    panel.messages = [
      ...panel.messages,
      {
        role: 'assistant',
        content: `**Error:** ${err?.message || String(err)}`,
      },
    ];
    rollbackUnstartedTurn(panel, requestId);
  }
}

/**
 * Undo the optimistic streaming state for a turn that never started.
 *
 * Both no-start paths need this: a transport rejection and an
 * `{status: "unsupported"}` slash reply. Neither will ever produce a
 * `streamComplete`, so nothing else is coming to clear the spinner or
 * stop the run timer.
 *
 * The optimistic *user* message is deliberately left in place. The user
 * did type it, and in the slash case the system note that follows only
 * makes sense underneath it.
 */
function rollbackUnstartedTurn(panel, requestId) {
  panel._streaming = false;
  panel._streamingContent = '';
  panel._currentRequestId = null;
  panel._streams.delete(requestId);
  panel._streamStartedAt = null;
  maybeStopStreamTimerTick(panel);
}

/**
 * Cancel the active stream. Best-effort — the
 * server may have already finished, so the cancel
 * call is fire-and-forget. Local cleanup happens
 * either way: the engine drains the interrupted turn
 * to its result rather than dropping it, so a
 * `streamComplete` still arrives — with
 * `terminal_reason` of `aborted_streaming` or
 * `aborted_tools`, handled uniformly in the streaming
 * module. Skipping that drain is what would route
 * this turn's tail into the next turn's UI.
 */
export async function cancel(panel) {
  if (!panel._streaming || !panel._currentRequestId) return;
  if (!panel.rpcConnected) return;
  try {
    await panel.rpcExtract(
      'ClaudeCodeService.cancel_streaming',
      panel._currentRequestId,
    );
  } catch (err) {
    console.warn('[chat] cancel_streaming failed', err);
    // Fall back to local cleanup.
    panel._streaming = false;
    panel._streamingContent = '';
    panel._currentRequestId = null;
    panel._streams.clear();
    panel._streamStartedAt = null;
    maybeStopStreamTimerTick(panel);
  }
}

// ---------------------------------------------------------------
// Session lifecycle
// ---------------------------------------------------------------
//
// Unreachable from the UI between phase 2 and phase 5: the ✨ and 📜
// buttons came off the action bar with the rest of the native engine's
// controls, because `ClaudeCodeService` had no session lifecycle to
// drive yet. It has one now, and the shape these handlers always had
// is the right one for it — call, then trust the broadcast.

/**
 * Start a new session.
 *
 * Nothing local is cleared here. The server broadcasts `sessionChanged`
 * with an empty message list, and the panel's handler resets the
 * transcript and streaming state from that — the same path a remote
 * client's new session takes, so every client ends up agreeing. A local
 * clear on the reply would make this client the one that jumped ahead.
 *
 * Two refusals come back rather than throw, and both are worth showing:
 * `turn_in_progress` (the server will not pull a session out from under
 * a live turn — the button is disabled while streaming, so reaching
 * this means the turn started underneath the click) and `restricted`
 * (the call is localhost-only; discarding the context every client is
 * looking at is the host's decision).
 */
export async function onNewSession(panel) {
  if (panel._streaming) return;
  if (!panel.rpcConnected) return;
  try {
    const result = await panel.rpcExtract(
      'ClaudeCodeService.new_session',
    );
    if (result && typeof result === 'object' && result.error) {
      const reason =
        result.error === 'restricted'
          ? result.reason || 'Only the host can start a new session'
          : result.error;
      panel._emitToast(reason, 'warning');
    }
    // Success says nothing: `session_id` is null until the CLI mints
    // one on the first turn, so there is nothing here to report that
    // the cleared transcript does not already say.
  } catch (err) {
    console.error('[chat] new_session failed', err);
    panel._emitToast(
      `Could not start a new session: ${err?.message || err}`,
      'warning',
    );
  }
}

export function onOpenHistory(panel) {
  panel._historyOpen = true;
}

export function onHistoryClose(panel) {
  panel._historyOpen = false;
}

/**
 * Load-session event from the history browser.
 * The server broadcasts `sessionChanged`
 * independently, so this handler's only job is
 * to close the modal — the message list is
 * replaced via the broadcast path.
 */
export function onHistorySessionLoaded(panel) {
  panel._historyOpen = false;
}

// ---------------------------------------------------------------
// Snippet drawer
// ---------------------------------------------------------------

export function toggleSnippetDrawer(panel) {
  panel._snippetDrawerOpen = !panel._snippetDrawerOpen;
  _saveDrawerOpen(panel._snippetDrawerOpen);
}

// `toggleReasoning` and `setReasoningEffort` stood here until conversion
// phase 3. Neither reached the wire after phase 2 — the ``reasoning`` and
// ``effort`` arguments they wrote are not on ``chat_streaming``'s new
// signature — and neither had a control on the action bar. See helpers.js for
// why their stored preferences could not be carried across.

/**
 * Insert a snippet's message into the textarea at
 * the current cursor position. If the textarea
 * has a selection, the selection is replaced.
 * Focuses the textarea after insertion so the
 * user can continue typing directly.
 */
export function insertSnippet(panel, snippet) {
  const message =
    snippet && typeof snippet.message === 'string'
      ? snippet.message
      : '';
  if (!message) return;
  const ta = panel.shadowRoot?.querySelector('.input-textarea');
  if (!ta) {
    panel._input = `${panel._input}${message}`;
    return;
  }
  // Compute the new value and cursor position
  // from the CURRENT textarea state (not
  // panel._input). If the user has been typing
  // fast, panel._input might lag by one input
  // event; reading directly from the textarea is
  // authoritative.
  const before = ta.value.slice(0, ta.selectionStart);
  const after = ta.value.slice(ta.selectionEnd);
  const next = `${before}${message}${after}`;
  panel._input = next;
  ta.value = next;
  const cursor = before.length + message.length;
  ta.setSelectionRange(cursor, cursor);
  ta.focus();
  // Fire an input event so the auto-resize logic
  // runs. Without this, inserting a multi-line
  // snippet doesn't grow the textarea until the
  // next keystroke.
  ta.dispatchEvent(new Event('input', { bubbles: true }));
}

// ---------------------------------------------------------------
// Textarea input
// ---------------------------------------------------------------

/**
 * Handle textarea `input` events. Updates state,
 * auto-resizes, and triggers @-mention detection.
 */
export function onInputChange(panel, event) {
  panel._input = event.target.value;
  // Persist the draft so a refresh / reload
  // doesn't lose unsent text. Cleared on send.
  _saveDraft(panel._input);
  // Auto-resize. Reset height first so shrinking
  // works when the user deletes content; then
  // measure and clamp to CSS max.
  const ta = event.target;
  ta.style.height = 'auto';
  ta.style.height = `${Math.min(ta.scrollHeight, 192)}px`;
  // @-filter detection. Runs after every input
  // event — we check whether the cursor is
  // inside an @word sequence and dispatch
  // edge-triggered `filter-from-chat` events.
  updateMentionFilter(panel, ta);
  // /-palette detection. Same shape, different
  // consumer: the palette is a child of this
  // component rather than a sibling panel, so it
  // is driven by direct method call instead of an
  // event.
  updateSlashPalette(panel, ta);
}

/**
 * Detect an active @mention at the cursor
 * position and dispatch `filter-from-chat` when
 * the mention state changes. Edge-triggered:
 *
 *   - Entering a mention: emit with the query.
 *   - Mention query changed: emit with new query.
 *   - Exiting a mention: emit empty query to clear
 *     the filter.
 *
 * A mention is `@` followed by zero or more
 * non-whitespace characters, with the cursor
 * INSIDE the sequence. The `@` must be at a word
 * boundary.
 */
export function updateMentionFilter(panel, ta) {
  const value = ta.value;
  const cursor = ta.selectionStart;
  const mention = detectActiveMention(value, cursor);
  if (mention === null && panel._activeMention === null) {
    return;
  }
  if (
    mention !== null &&
    panel._activeMention !== null &&
    mention.start === panel._activeMention.start &&
    mention.end === panel._activeMention.end &&
    mention.query === panel._activeMention.query
  ) {
    return;
  }
  panel._activeMention = mention;
  const query = mention === null ? '' : mention.query;
  panel.dispatchEvent(
    new CustomEvent('filter-from-chat', {
      detail: { query },
      bubbles: true,
      composed: true,
    }),
  );
}

/**
 * Walk backward from the cursor to find an active
 * @mention. Returns `{start, end, query}` or
 * null.
 *
 * Returns null when:
 *   - No `@` found before cursor at word boundary
 *   - Whitespace between the `@` and cursor
 *     (mention terminated)
 *   - `@` is preceded by a word char (blocks
 *     `foo@bar` from matching)
 */
export function detectActiveMention(value, cursor) {
  for (let i = cursor - 1; i >= 0; i -= 1) {
    const ch = value[i];
    if (/\s/.test(ch)) {
      return null;
    }
    if (ch === '@') {
      const before = i > 0 ? value[i - 1] : '';
      if (before !== '' && !/\s/.test(before)) {
        // @ embedded in a word (email-like).
        return null;
      }
      return {
        start: i,
        end: cursor,
        query: value.slice(i + 1, cursor),
      };
    }
  }
  return null;
}

// ---------------------------------------------------------------
// Slash commands
// ---------------------------------------------------------------

// How long to wait before asking for the command list again
// after a failed attempt, so a broken engine does not get one
// RPC per keystroke. Only true failures are stamped: a
// disconnected engine is not one, and answers with the routed
// commands and `partial: true` (see `list_commands`).
const _SLASH_RETRY_MS = 5000;

/**
 * Open, re-filter, or dismiss the `/` palette to match the
 * cursor. Called on every input event.
 *
 * The list is fetched on first use rather than at connect:
 * it comes from the CLI's initialize handshake, so asking
 * eagerly would either race the engine coming up or cache
 * an empty answer from before it did.
 */
export function updateSlashPalette(panel, ta) {
  const palette = panel.shadowRoot?.querySelector('ac-slash-palette');
  if (!palette) return;
  const token = detectActiveSlash(ta.value, ta.selectionStart);
  if (token === null) {
    palette.hide();
    return;
  }
  if (Array.isArray(panel._slashCommands) && !slashListIsStale(panel)) {
    // An empty list means the engine advertised nothing —
    // show no overlay rather than one that says "0 of 0".
    if (panel._slashCommands.length === 0) palette.hide();
    else palette.show(panel._slashCommands, token.query);
    return;
  }
  // Serve the stale list meanwhile: it is the routed commands,
  // which are still correct, and an overlay that appears late
  // is worse than one that grows.
  if (panel._slashCommands?.length) {
    palette.show(panel._slashCommands, token.query);
  }
  ensureSlashCommands(panel).then((commands) => {
    if (commands.length === 0) return;
    // Re-read the composer: the RPC is a round trip, and the
    // token may be gone or changed by the time it lands.
    const current = detectActiveSlash(ta.value, ta.selectionStart);
    if (current) palette.show(commands, current.query);
  });
}

/**
 * Whether the cached list needs asking again.
 *
 * A `partial` list is the routed commands and nothing else: the
 * engine had not connected, so there was no handshake to read
 * the CLI's own commands from. The engine connects on the first
 * turn — which is the turn the user is composing when they open
 * the palette — so the answer changes underneath the cache
 * exactly once, and this is the pull-side check for it. Asked
 * here rather than invalidated from the `engineHealth`
 * broadcast so there is one place that decides, and nothing to
 * miss if a broadcast is dropped.
 */
function slashListIsStale(panel) {
  return (
    panel._slashCommandsPartial === true &&
    panel._engineHealth?.connected === true
  );
}

/**
 * The command list, fetched on first use and cached.
 *
 * Concurrent callers share one in-flight request — every
 * keystroke of `/con` would otherwise open its own. A partial
 * reply is cached too, so a disconnected engine costs one RPC
 * rather than one per keystroke; `slashListIsStale` is what
 * gets it replaced. A true failure is not cached, only stamped.
 */
async function ensureSlashCommands(panel) {
  if (Array.isArray(panel._slashCommands) && !slashListIsStale(panel)) {
    return panel._slashCommands;
  }
  if (panel._slashCommandsPending) return panel._slashCommandsPending;
  const lastTry = panel._slashCommandsFailedAt || 0;
  if (lastTry && Date.now() - lastTry < _SLASH_RETRY_MS) return [];
  const pending = (async () => {
    try {
      const reply = await panel.rpcExtract('ClaudeCodeService.list_commands');
      if (reply && typeof reply === 'object' && reply.error) {
        panel._slashCommandsFailedAt = Date.now();
        return [];
      }
      const commands = Array.isArray(reply?.commands) ? reply.commands : [];
      panel._slashCommands = commands;
      panel._slashCommandsPartial = reply?.partial === true;
      panel._slashCommandsFailedAt = 0;
      return commands;
    } catch (err) {
      console.error('[chat] list_commands failed', err);
      panel._slashCommandsFailedAt = Date.now();
      return [];
    } finally {
      panel._slashCommandsPending = null;
    }
  })();
  panel._slashCommandsPending = pending;
  return pending;
}

/**
 * Handle `command-select` from the palette.
 *
 * Two outcomes, and the entry said which in its badge. A
 * `route` command never becomes text: its surface opens now
 * and the token is taken back out of the composer, because
 * leaving `/context` sitting there after the Context tab has
 * opened invites a second, pointless Enter.
 *
 * A `send` command is completed in place and left for the
 * user to submit — they may still have arguments to add, and
 * the argument hint is the palette telling them so.
 */
export function onSlashCommandSelect(panel, event) {
  const command = event?.detail?.command;
  if (!command?.name) return;
  const ta = panel.shadowRoot?.querySelector('.input-textarea');
  const value = ta ? ta.value : panel._input || '';
  const cursor = ta ? ta.selectionStart : value.length;
  const token = detectActiveSlash(value, cursor);
  const before = token ? value.slice(0, token.start) : '';
  const after = token ? value.slice(token.end) : '';
  if (command.action === 'route') {
    _setComposerValue(panel, ta, `${before}${after}`.trimStart(), 0);
    applySlashRoute(panel, command.target);
    return;
  }
  const completion = completionFor(command);
  _setComposerValue(
    panel,
    ta,
    `${before}${completion}${after}`,
    before.length + completion.length,
  );
}

/**
 * Replace the composer's contents and put the cursor at
 * `caret`.
 *
 * Writes through the textarea as well as `panel._input`
 * because the caret has to be set on the live element, and
 * Lit's next render would otherwise clobber it. Same reason
 * the draft is saved here: this is a composer edit like any
 * keystroke, and a refresh should not lose it.
 */
function _setComposerValue(panel, ta, value, caret) {
  panel._input = value;
  _saveDraft(value);
  if (!ta) return;
  ta.value = value;
  ta.focus();
  ta.setSelectionRange(caret, caret);
  ta.style.height = 'auto';
  ta.style.height = `${Math.min(ta.scrollHeight, 192)}px`;
}

/**
 * Open the AC⚡DC surface a routed command names.
 *
 * The `target` strings come from `SLASH_ROUTES` in the
 * service, so the mapping from command to surface lives in
 * one place and this function only knows how to reach each
 * surface. An unrecognised target is ignored rather than
 * guessed at — a service that grows a new route without this
 * switch learning it should do nothing visible, not the
 * wrong thing.
 */
export function applySlashRoute(panel, target) {
  switch (target) {
    case 'tab:context':
    case 'tab:settings':
      panel.dispatchEvent(
        new CustomEvent('request-dialog-tab', {
          detail: { tab: target.slice('tab:'.length) },
          bubbles: true,
          composed: true,
        }),
      );
      return true;
    case 'new-session':
      onNewSession(panel);
      return true;
    case 'history':
      onOpenHistory(panel);
      return true;
    default:
      console.warn('[chat] unknown slash route target', target);
      return false;
  }
}

/**
 * A routed command that was typed rather than picked.
 *
 * Sits here rather than beside `handleUnsupportedSlash` in
 * streaming.js because acting on the route needs
 * `onNewSession` and `onOpenHistory`, which live in this
 * module — and streaming.js is already imported *by* this
 * one, so the dependency could only go this way.
 *
 * The note is appended even though the surface is opening,
 * because the surface may be behind the chat dialog or may
 * already have been open, and a command that appears to do
 * nothing is worse than one that explains itself.
 */
function handleRoutedSlash(panel, result) {
  applySlashRoute(panel, result.target);
  const message =
    typeof result?.message === 'string' && result.message
      ? result.message
      : `/${result?.command || 'that'} opens an AC⚡DC surface here.`;
  panel.messages = [
    ...panel.messages,
    { role: 'system', content: message },
  ];
}

/**
 * Handle key events on the textarea. Up-arrow at
 * cursor 0 opens history recall; Enter sends
 * (Shift+Enter inserts a newline). The history
 * overlay and then the slash palette get first
 * refusal on navigation keys when open.
 *
 * `event.isComposing` guards against premature
 * send during IME input (Japanese/Chinese input
 * methods).
 */
export function onInputKeyDown(panel, event) {
  const history = panel.shadowRoot?.querySelector(
    'ac-input-history',
  );
  if (history && history.isOpen) {
    if (history.handleKey(event)) return;
  }
  // The palette is only open while the cursor sits inside a
  // leading `/token`, so it can never be up at the same time
  // as history recall (which needs cursor 0, where the token
  // detection returns null). Ordered after it anyway: history
  // is the modal one, and "whoever is open wins" is easier to
  // reason about than a rule about which can't overlap.
  const palette = panel.shadowRoot?.querySelector('ac-slash-palette');
  if (palette && palette.isOpen) {
    if (palette.handleKey(event)) return;
  }
  if (
    event.key === 'ArrowUp' &&
    !event.shiftKey &&
    !event.ctrlKey &&
    !event.metaKey &&
    event.target.selectionStart === 0 &&
    event.target.selectionEnd === 0
  ) {
    if (history && history.show(panel._input)) {
      event.preventDefault();
      return;
    }
  }
  if (
    event.key === 'Enter'
    && !event.shiftKey
    && !event.isComposing
  ) {
    event.preventDefault();
    send(panel);
  }
}

/**
 * Handle `history-select` from the input-history
 * component. The event carries the selected text
 * and any images that were attached when the
 * message was originally sent. We replace the
 * textarea content with the text, replace the
 * pending-image strip with the recalled images,
 * and focus the textarea so the user can edit
 * before re-sending.
 *
 * Image restore goes through `addPendingImage`
 * one-by-one so the standard limit / dedup /
 * size checks apply uniformly. If a stored
 * image now exceeds the size cap (e.g. config
 * was tightened since the original send) it
 * silently drops with a toast — better than
 * surfacing a recalled message in an
 * un-sendable state.
 */
export function onHistorySelect(panel, event) {
  const text = event.detail?.text ?? '';
  const images = Array.isArray(event.detail?.images)
    ? event.detail.images
    : [];
  panel._input = text;
  // Replace pending images with the recalled set.
  // Clear first so a recall doesn't accumulate
  // on top of whatever the user had drafted.
  panel._pendingImages = [];
  for (const dataUri of images) {
    addPendingImage(panel, dataUri);
  }
  panel.updateComplete.then(() => {
    const ta = panel.shadowRoot?.querySelector('.input-textarea');
    if (ta) {
      ta.focus();
      ta.setSelectionRange(text.length, text.length);
      ta.style.height = 'auto';
      ta.style.height = `${Math.min(ta.scrollHeight, 192)}px`;
    }
  });
}

/**
 * Handle `history-cancel`. The detail carries
 * the saved original input; we restore it
 * verbatim so Escape feels like an undo.
 */
export function onHistoryCancel(panel, event) {
  const text = event.detail?.text ?? '';
  panel._input = text;
  panel.updateComplete.then(() => {
    const ta = panel.shadowRoot?.querySelector('.input-textarea');
    if (ta) {
      ta.focus();
      ta.setSelectionRange(text.length, text.length);
    }
  });
}

// ---------------------------------------------------------------
// Paste + images
// ---------------------------------------------------------------

/**
 * Handle paste events on the textarea. If the
 * clipboard contains image items, consume them
 * and add to the pending list. Text pastes fall
 * through to the textarea's native behaviour —
 * don't preventDefault unless we actually
 * captured at least one image.
 *
 * The one-shot `_suppressNextPaste` flag is set
 * by the files-tab's middle-click-insert flow to
 * block the Linux selection-buffer auto-paste
 * that follows the focus() call. The flag clears
 * on the same event, so a subsequent user paste
 * (Ctrl+V or right-click → paste) flows through
 * normally.
 */
export async function onInputPaste(panel, event) {
  if (panel._suppressNextPaste) {
    panel._suppressNextPaste = false;
    event.preventDefault();
    return;
  }
  const cb = event.clipboardData;
  if (!cb) return;
  const images = await extractImagesFromClipboard(cb);
  if (images.length === 0) return;
  // Consume the paste event so the browser
  // doesn't additionally try to paste a `[object
  // Object]` string representation.
  event.preventDefault();
  for (const dataUri of images) {
    addPendingImage(panel, dataUri);
  }
}

/**
 * Add a data URI to the pending images list.
 * Shared between paste and re-attach paths.
 * Enforces:
 *   - MAX_IMAGES_PER_MESSAGE
 *   - MAX_IMAGE_BYTES
 *   - Dedup by exact data URI
 *
 * Returns true if the image was added, false if
 * rejected.
 */
export function addPendingImage(panel, dataUri) {
  if (typeof dataUri !== 'string' || !dataUri) return false;
  if (panel._pendingImages.includes(dataUri)) return false;
  if (panel._pendingImages.length >= MAX_IMAGES_PER_MESSAGE) {
    panel._emitToast(
      `Maximum ${MAX_IMAGES_PER_MESSAGE} images per message`,
      'warning',
    );
    return false;
  }
  const bytes = estimateDataUriBytes(dataUri);
  if (bytes > MAX_IMAGE_BYTES) {
    const mb = Math.round(MAX_IMAGE_BYTES / (1024 * 1024));
    panel._emitToast(
      `Image exceeds ${mb} MiB limit`,
      'warning',
    );
    return false;
  }
  panel._pendingImages = [...panel._pendingImages, dataUri];
  return true;
}

/** Remove a pending image by index. */
export function removePendingImage(panel, index) {
  if (index < 0 || index >= panel._pendingImages.length) return;
  panel._pendingImages = [
    ...panel._pendingImages.slice(0, index),
    ...panel._pendingImages.slice(index + 1),
  ];
}

/**
 * Re-attach an image from a past message to the
 * current composition. Goes through the same
 * `addPendingImage` path so limit checks and
 * dedup apply uniformly. Emits a confirmation
 * toast on success since the visual feedback
 * (image appears in thumbnail strip below
 * textarea) may not be visible if the user is
 * scrolled up in the message list.
 */
export function reattachImage(panel, dataUri) {
  const wasAlreadyAttached = panel._pendingImages.includes(dataUri);
  if (addPendingImage(panel, dataUri)) {
    panel._emitToast('Image attached', 'success');
  } else if (wasAlreadyAttached) {
    panel._emitToast('Image already attached', 'info');
  }
}

export function openLightbox(panel, dataUri) {
  panel._lightboxImage = dataUri;
}

export function closeLightbox(panel) {
  panel._lightboxImage = null;
}

export function onLightboxKeyDown(panel, event) {
  if (event.key === 'Escape') {
    event.preventDefault();
    closeLightbox(panel);
  }
}

// ---------------------------------------------------------------
// Speech-to-text
// ---------------------------------------------------------------

/**
 * Insert a transcribed speech segment at the
 * textarea's cursor position. Adds space
 * separators when adjacent text is non-whitespace
 * so successive utterances don't jam together
 * ("helloworld") and so dictation mid-sentence
 * inserts cleanly.
 *
 * Per specs4/5-webapp/speech.md — existing input
 * is preserved (never overwritten); cursor ends
 * up after the inserted text.
 */
export function onTranscript(panel, event) {
  const text = event.detail?.text;
  if (typeof text !== 'string' || !text) return;
  const ta = panel.shadowRoot?.querySelector('.input-textarea');
  if (!ta) {
    panel._input = `${panel._input}${text}`;
    return;
  }
  const before = ta.value.slice(0, ta.selectionStart);
  const after = ta.value.slice(ta.selectionEnd);
  // Auto-space: prepend a space if the char
  // before the cursor is non-whitespace, append
  // one if the char after is non-whitespace.
  // Mid-word dictation ("I am goinghome") would
  // otherwise be a garden-path parse problem
  // for the reader.
  const prefix =
    before.length > 0 && !/\s$/.test(before) ? ' ' : '';
  const suffix =
    after.length > 0 && !/^\s/.test(after) ? ' ' : '';
  const insertion = `${prefix}${text}${suffix}`;
  const next = `${before}${insertion}${after}`;
  panel._input = next;
  ta.value = next;
  const cursor = before.length + insertion.length;
  ta.setSelectionRange(cursor, cursor);
  ta.dispatchEvent(new Event('input', { bubbles: true }));
}

/**
 * Surface a speech recognition error as a toast.
 * The component has already reverted to inactive
 * state by the time this fires.
 */
export function onRecognitionError(panel, event) {
  const errorCode = event.detail?.error || 'unknown';
  const messages = {
    'not-allowed': 'Microphone access denied',
    'service-not-allowed': 'Speech service unavailable',
    'audio-capture': 'No microphone detected',
    network: 'Speech recognition network error',
  };
  const message =
    messages[errorCode] || `Speech error: ${errorCode}`;
  panel._emitToast(message, 'warning');
}

// ---------------------------------------------------------------
// Message text extraction (copy / paste actions)
// ---------------------------------------------------------------

/**
 * Extract raw text from a message for copy /
 * paste actions. Handles both string and
 * multimodal-array content shapes — the backend
 * sends multimodal arrays for session-reloaded
 * messages that had images, plain strings for
 * everything else.
 *
 * Images are dropped — this is a text action.
 */
export function extractMessageText(msg) {
  if (!msg) return '';
  const raw = msg.content;
  if (typeof raw === 'string') return raw;
  if (Array.isArray(raw)) {
    const parts = [];
    for (const block of raw) {
      if (
        block &&
        block.type === 'text' &&
        typeof block.text === 'string'
      ) {
        parts.push(block.text);
      }
    }
    return parts.join('\n');
  }
  return '';
}

/**
 * Copy the message's raw text to the clipboard.
 * Toast on success/failure since the clipboard
 * write is silent otherwise.
 */
export async function copyMessageText(panel, msg) {
  const text = extractMessageText(msg);
  if (!text) {
    // Probably an image-only message. Silent
    // rather than emitting a noisy warning.
    return;
  }
  try {
    if (
      navigator.clipboard &&
      typeof navigator.clipboard.writeText === 'function'
    ) {
      await navigator.clipboard.writeText(text);
      panel._emitToast('Copied to clipboard', 'success');
    } else {
      panel._emitToast('Clipboard not available', 'warning');
    }
  } catch (err) {
    panel._emitToast(
      `Copy failed: ${err?.message || 'permission denied'}`,
      'warning',
    );
  }
}

/**
 * Insert the message's raw text into the chat
 * input at the current cursor position. Replaces
 * any selection. Focuses the textarea after.
 */
export function pasteMessageToPrompt(panel, msg) {
  const text = extractMessageText(msg);
  if (!text) return;
  const ta = panel.shadowRoot?.querySelector('.input-textarea');
  if (!ta) {
    panel._input = `${panel._input}${text}`;
    return;
  }
  const before = ta.value.slice(0, ta.selectionStart);
  const after = ta.value.slice(ta.selectionEnd);
  const next = `${before}${text}${after}`;
  panel._input = next;
  ta.value = next;
  const cursor = before.length + text.length;
  ta.setSelectionRange(cursor, cursor);
  ta.focus();
  ta.dispatchEvent(new Event('input', { bubbles: true }));
}

// ---------------------------------------------------------------
// Text-to-speech (read message aloud)
// ---------------------------------------------------------------

/**
 * Read a message aloud via the Web Speech synthesis API.
 *
 * Smart-scope behaviour (per the speak-control design):
 * if the user has a non-empty text selection inside THIS
 * message card, only the selection is read; otherwise the
 * whole message is read.
 *
 * Toggles: clicking the speaker on the message that's
 * currently playing stops playback. Because
 * `speechSynthesis` is a single window-level queue,
 * starting a read on any message cancels whatever was
 * playing before (handled inside the player), and
 * `panel._speakingMsgIndex` tracks which card owns the
 * active utterance so its button can show the stop state.
 * That index is kept in sync by the panel's
 * `speech-player-state` listener (events.js) rather than
 * set here, so a read started from anywhere — or stopped
 * via the floating controls — lights the right button.
 *
 * Playback is delegated to `speechPlayer`, which splits
 * the text into sentences and plays them in sequence so
 * the floating transport (ac-speech-controls) can offer
 * play/pause, speed, and per-sentence position.
 *
 * `cardEl` is the `.message-card` element — passed from
 * the toolbar click handler so we can read the rendered
 * (markdown-stripped) text the user actually sees rather
 * than the raw markdown source.
 */
export function speakMessage(panel, msg, index, cardEl) {
  if (!isSpeechSynthesisSupported()) {
    panel._emitToast(
      'Text-to-speech is not supported in this browser',
      'warning',
    );
    return;
  }
  // Clicking the active speaker stops it.
  if (panel._speakingMsgIndex === index) {
    speechPlayer.stop();
    return;
  }
  const text = resolveSpeechText(msg, cardEl, panel);
  if (!text) return;
  // ownerKey is the message index so the panel's state
  // listener can light this card's speaker button; label
  // gives the floating controls a human header.
  speechPlayer.play(text, {
    ownerKey: index,
    label: speechLabelFor(msg, index),
  });
}

/**
 * Human-readable header for the floating controls — the
 * message's role plus its position, e.g. "Assistant · #4".
 * Falls back gracefully for system events and unknown
 * roles.
 */
function speechLabelFor(msg, index) {
  const role = msg?.system_event
    ? 'System'
    : msg?.role === 'assistant'
      ? 'Assistant'
      : msg?.role === 'user'
        ? 'You'
        : 'Message';
  return `${role} · #${index + 1}`;
}

/**
 * Resolve the text to read for a message. Prefers a
 * selection within the card, then the rendered prose,
 * then the raw extracted text as a last resort.
 */
function resolveSpeechText(msg, cardEl, panel) {
  const selected = getSelectedTextWithin(panel, cardEl);
  if (selected) return selected;
  // Whole-message read: gather the rendered prose blocks
  // (.md-content) so markdown syntax, edit-block code, and
  // agent-card chrome are excluded — we read what the user
  // reads, not the raw source.
  if (cardEl) {
    const proseNodes = cardEl.querySelectorAll('.md-content');
    if (proseNodes.length > 0) {
      const text = Array.from(proseNodes)
        .map((n) => n.textContent || '')
        .join('\n')
        .trim();
      if (text) return text;
    }
  }
  // No rendered DOM available (e.g. unit tests) — fall back
  // to the raw content, markdown and all.
  return extractMessageText(msg);
}

/**
 * Return the trimmed selected text when there's a
 * non-empty selection that lies within `cardEl`, else ''.
 *
 * Shadow-DOM caveat: Chromium exposes
 * `shadowRoot.getSelection()`, which scopes correctly to
 * nodes inside the shadow tree. Firefox/Safari only offer
 * `window.getSelection()`, where the anchor may be
 * retargeted to the shadow host — in that case the
 * containment check fails and we fall back to reading the
 * whole message. That's the intended graceful
 * degradation: the button always works, selection-only
 * reading is a Chromium enhancement.
 */
function getSelectedTextWithin(panel, cardEl) {
  if (!cardEl) return '';
  let selection = null;
  const root = panel?.shadowRoot;
  if (root && typeof root.getSelection === 'function') {
    selection = root.getSelection();
  } else if (
    typeof window !== 'undefined' &&
    typeof window.getSelection === 'function'
  ) {
    selection = window.getSelection();
  }
  if (!selection) return '';
  const text = selection.toString();
  if (!text || !text.trim()) return '';
  // Confirm the selection lies within this card so a
  // selection in a different message isn't read when this
  // card's speaker is clicked.
  const anchor = selection.anchorNode;
  const focus = selection.focusNode;
  const within =
    (anchor && cardEl.contains(anchor)) ||
    (focus && cardEl.contains(focus));
  if (!within) return '';
  return text.trim();
}

// ---------------------------------------------------------------
// Auto-scroll engagement
// ---------------------------------------------------------------

/**
 * Scroll listener on the messages container.
 * Engages auto-scroll when the user is at (or
 * very close to) the bottom; disengages when
 * they scroll up past the disengage threshold.
 *
 * Two thresholds (engage and disengage) prevent
 * flicker between states from sub-pixel scroll
 * events during smooth scrolling.
 */
export function onMessagesScroll(panel, event) {
  const el = event.currentTarget;
  const distanceFromBottom =
    el.scrollHeight - el.scrollTop - el.clientHeight;
  if (distanceFromBottom > AUTO_SCROLL_DISENGAGE_PX) {
    panel._autoScroll = false;
  } else if (distanceFromBottom <= AUTO_SCROLL_TOLERANCE_PX) {
    panel._autoScroll = true;
  }
}

/**
 * Whether the live conversation has messages the user has not read to the
 * end of.
 *
 * Asked by the history browser, which is about to replace this conversation
 * and offers a second click when the answer is yes
 * (specs5/5-webapp/chat.md § Resume Is Not Load).
 *
 * Two ways to not have seen the end. A turn in flight means the end has not
 * been written yet; otherwise it is a question of where the reader is, and
 * `_autoScroll` is the panel's own running answer to "is the bottom on
 * screen" — the flag that decides whether a new message scrolls into view or
 * waits above the fold. An empty transcript has no end to miss.
 */
export function hasUnreadLiveMessages(panel) {
  const messages = panel.messages;
  if (!Array.isArray(messages) || messages.length === 0) return false;
  if (panel._streaming) return true;
  return panel._autoScroll === false;
}

/**
 * Single click listener on the messages
 * container — event delegation for three kinds of
 * target:
 *
 *   1. `.file-mention` inside assistant prose —
 *      toggle file selection + navigate to diff
 *      viewer.
 *   2. `.code-copy-btn` inside a rendered code
 *      block — copy the sibling `<code>`'s
 *      textContent to the clipboard and flash a
 *      ✓ indicator.
 *   3. `.edit-file-path` inside an edit-block
 *      card — navigate to the file in the diff
 *      viewer, scrolling to the edit anchor.
 *
 * Delegation pattern rather than per-span
 * handlers so lit-html's template diffing
 * doesn't need to track handler attachment per
 * span — the wrapped HTML comes from
 * `unsafeHTML` and doesn't participate in Lit's
 * event binding anyway.
 */
export function onMessagesClick(panel, event) {
  const target = event.target;
  if (!target || typeof target.closest !== 'function') return;

  // Copy-code button. Handled first so a copy
  // button nested inside some exotic parent
  // structure doesn't fall through to other
  // handlers.
  const copyBtn = target.closest('.code-copy-btn');
  if (copyBtn) {
    event.preventDefault();
    event.stopPropagation();
    handleCodeCopy(panel, copyBtn);
    return;
  }

  // Edit-block file path — navigate with anchor
  // text.
  const editPath = target.closest('.edit-file-path');
  if (editPath) {
    const path = editPath.getAttribute('data-edit-path');
    if (path) {
      event.preventDefault();
      event.stopPropagation();
      const anchor = editPath.getAttribute('data-edit-anchor') || '';
      window.dispatchEvent(
        new CustomEvent('navigate-file', {
          detail: {
            path,
            ...(anchor ? { searchText: anchor } : {}),
          },
          bubbles: false,
        }),
      );
    }
    return;
  }

  // File mention inside prose.
  if (
    !target.classList ||
    !target.classList.contains('file-mention')
  ) {
    return;
  }
  const path = target.getAttribute('data-file');
  if (!path) return;
  event.preventDefault();
  event.stopPropagation();
  panel.dispatchEvent(
    new CustomEvent('file-mention-click', {
      detail: { path },
      bubbles: true,
      composed: true,
    }),
  );
}

/**
 * Copy the contents of the `<code>` element
 * inside the same `<pre>` as the clicked button.
 * Flashes a ✓ via a temporary `.copied` class
 * for 1.5s, with a toast on failure.
 */
export async function handleCodeCopy(panel, copyBtn) {
  const pre = copyBtn.closest('pre.code-block');
  if (!pre) return;
  const codeEl = pre.querySelector('code');
  if (!codeEl) return;
  const text = codeEl.textContent || '';
  if (!text) return;
  try {
    if (
      navigator.clipboard &&
      typeof navigator.clipboard.writeText === 'function'
    ) {
      await navigator.clipboard.writeText(text);
      // Flash ✓ by swapping the icon content for
      // 1.5s. Preserve the original innerHTML so
      // the SVG icon comes back cleanly.
      const originalHtml = copyBtn.innerHTML;
      copyBtn.textContent = '✓';
      copyBtn.classList.add('copied');
      setTimeout(() => {
        copyBtn.classList.remove('copied');
        copyBtn.innerHTML = originalHtml;
      }, 1500);
    } else {
      panel._emitToast('Clipboard not available', 'warning');
    }
  } catch (err) {
    panel._emitToast(
      `Copy failed: ${err?.message || 'permission denied'}`,
      'warning',
    );
  }
}

// ---------------------------------------------------------------
// File chip click + Add-All
// ---------------------------------------------------------------

/**
 * Handle a chip click — dispatch
 * `file-chip-click` with `navigate: false` so
 * the files-tab toggles selection without
 * opening the file in the viewer.
 *
 * When adding a not-in-context file (not
 * removing an in-context one), accumulate
 * natural-language text in the chat input per
 * spec.
 */
export function onFileChipClick(panel, path) {
  if (typeof path !== 'string' || !path) return;
  const selected = new Set(
    Array.isArray(panel.selectedFiles) ? panel.selectedFiles : [],
  );
  const isAdd = !selected.has(path);
  panel.dispatchEvent(
    new CustomEvent('file-chip-click', {
      detail: { path, navigate: false },
      bubbles: true,
      composed: true,
    }),
  );
  if (isAdd) {
    accumulateAddedFilesInInput(panel, [path]);
  }
}

/**
 * Accumulate natural-language text into the chat
 * input announcing files the user just added to
 * context.
 *
 * Per specs4/5-webapp/chat.md §Input
 * Accumulation on Add:
 *   - Templates — "The file X added. Do you
 *     want to see more files before you
 *     continue?" for the first add; updated to
 *     join multiple files naturally on
 *     subsequent adds.
 *   - Only basename used in accumulated text.
 *   - Falls back to appending a parenthetical
 *     note for non-matching input states.
 *
 * The "matching input state" is text that
 * already follows the generated template — we
 * splice additional filenames into the existing
 * phrase. Anything else (the user typed their
 * own message, or the phrasing diverged) falls
 * back to a parenthetical note appended at the
 * end so we don't rewrite user content.
 */
export function accumulateAddedFilesInInput(panel, paths) {
  if (!Array.isArray(paths) || paths.length === 0) return;
  // Basename only per spec — trailing segment
  // after the last slash. Works for both forward
  // and back slashes so Windows-style paths
  // don't slip through.
  const toBasename = (p) => {
    const idx = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'));
    return idx >= 0 ? p.slice(idx + 1) : p;
  };
  const newNames = paths
    .map(toBasename)
    .filter((n) => typeof n === 'string' && n);
  if (newNames.length === 0) return;

  const current = panel._input;
  const trailing =
    ' Do you want to see more files before you continue?';

  // Detect an existing accumulated phrase we can
  // extend. Matches both singular ("The file X
  // added.") and plural ("The files X, Y
  // added.") forms followed by the trailing
  // question.
  const existingRe =
    /^The files? ([^.]+?) added\. Do you want to see more files before you continue\?\s*$/;
  const match = current.match(existingRe);

  let next;
  if (match) {
    const existing = match[1]
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);
    const seen = new Set(existing);
    const merged = [...existing];
    for (const name of newNames) {
      if (!seen.has(name)) {
        seen.add(name);
        merged.push(name);
      }
    }
    const noun = merged.length === 1 ? 'file' : 'files';
    next = `The ${noun} ${merged.join(', ')} added.${trailing}`;
  } else if (current.trim() === '') {
    const noun = newNames.length === 1 ? 'file' : 'files';
    next = `The ${noun} ${newNames.join(', ')} added.${trailing}`;
  } else {
    // Non-matching input — user typed something
    // of their own. Don't rewrite their text;
    // append a parenthetical note.
    const noun = newNames.length === 1 ? 'file' : 'files';
    const suffix = ` (${noun} added: ${newNames.join(', ')})`;
    next = current + suffix;
  }

  panel._input = next;
  panel.updateComplete.then(() => {
    const ta = panel.shadowRoot?.querySelector('.input-textarea');
    if (!ta) return;
    ta.value = next;
    ta.setSelectionRange(next.length, next.length);
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

/**
 * "Add All" button handler. Dispatches
 * `file-chips-add-all` with `{paths: [...]}`
 * carrying the list of not-in-context paths. The
 * files-tab handler batches them into a single
 * `set_selected_files` call.
 *
 * Also accumulates natural-language text in the
 * chat input — same path as single-chip add.
 */
export function onAddAllFiles(panel, notInContext) {
  if (!Array.isArray(notInContext) || notInContext.length === 0) return;
  const paths = notInContext
    .map((f) => f.path)
    .filter((p) => typeof p === 'string' && p);
  if (paths.length === 0) return;
  panel.dispatchEvent(
    new CustomEvent('file-chips-add-all', {
      detail: { paths },
      bubbles: true,
      composed: true,
    }),
  );
  accumulateAddedFilesInInput(panel, paths);
}

// ---------------------------------------------------------------
// Auto-scroll target
// ---------------------------------------------------------------

/**
 * Scroll the messages container to the bottom.
 * Double rAF — wait for Lit's DOM commit, then
 * one more frame for browser layout to settle
 * before measuring scrollHeight. Without this,
 * the first chunk of a stream sometimes scrolls
 * to stale dimensions.
 */
export function scrollToBottom(panel) {
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      const container = panel.shadowRoot?.querySelector(
        '.messages',
      );
      if (container) {
        container.scrollTop = container.scrollHeight;
      }
    });
  });
}