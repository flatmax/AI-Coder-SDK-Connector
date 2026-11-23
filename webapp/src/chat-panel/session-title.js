// The current session's title, in the action bar, and the click that renames it.
//
// The title is the SDK's `custom_title`: one the user gave, else the one the
// CLI generates during the first turn. So a session usually has a name
// without anyone renaming it, and a rename overrides it everywhere the
// session is listed — `history_rename` appends the same `custom-title`
// entry the CLI's `/rename` writes.
//
// Self-contained: it keeps its own listeners rather than threading a call
// through four handlers that each own something else. What it listens to
// is every way the session on screen can change or be named:
//
//   state-loaded      — connect/reconnect; carries `session_id`.
//   session-started   — the engine's init, the first time an ID exists.
//   session-changed   — resume, fork, or new (`session_id` null).
//   stream-complete   — a turn ended; the CLI may have titled it by now.
//   session-labelled  — renamed or tagged here or by another client.

import { html, nothing } from 'lit';
import { withRpcTimeout } from '../rpc.js';

const LABEL_TIMEOUT_MS = 15000;

/** Track `sessionId` as the session on screen, and fetch its label. */
export async function syncSessionLabel(panel, sessionId) {
  if (!sessionId) {
    panel._sessionLabel = null;
    panel._sessionTitleEditing = false;
    return;
  }
  if (panel._sessionLabel?.session_id !== sessionId) {
    panel._sessionLabel = { session_id: sessionId, title: '', tag: '' };
    panel._sessionTitleEditing = false;
  }
  if (!panel.rpcConnected) return;
  let label;
  try {
    label = await withRpcTimeout(
      panel.rpcExtract('ClaudeCodeService.history_label', sessionId),
      LABEL_TIMEOUT_MS,
      'history_label',
    );
  } catch (err) {
    // A chip that says "Untitled" is the honest fallback; nothing to toast.
    console.debug('[session-title] history_label failed', err);
    return;
  }
  // The session may have changed while the read was in flight.
  if (!label || label.error) return;
  if (panel._sessionLabel?.session_id !== sessionId) return;
  panel._sessionLabel = {
    session_id: sessionId,
    title: label.title || '',
    tag: label.tag || '',
  };
}

export function onSessionLabelled(panel, event) {
  const label = event?.detail;
  if (!label?.session_id) return;
  if (panel._sessionLabel?.session_id !== label.session_id) return;
  panel._sessionLabel = {
    session_id: label.session_id,
    title: label.title || '',
    tag: label.tag || '',
  };
}

/**
 * After a turn, re-read only an untitled session. The CLI titles a session
 * during its first turn, and after that the only change is a rename, which
 * arrives as `session-labelled` — so a titled session costs no reads.
 */
function onTurnEnded(panel) {
  const current = panel._sessionLabel;
  if (current && !current.title) syncSessionLabel(panel, current.session_id);
}

export function attachSessionTitle(panel) {
  panel._sessionTitleListeners = {
    'state-loaded': (e) => syncSessionLabel(panel, e.detail?.session_id),
    'session-started': (e) =>
      syncSessionLabel(panel, e.detail?.data?.session_id),
    // Null on `new`: that session has no ID until its first turn starts.
    'session-changed': (e) => syncSessionLabel(panel, e.detail?.session_id),
    'stream-complete': () => onTurnEnded(panel),
    'session-labelled': (e) => onSessionLabelled(panel, e),
  };
  for (const [name, fn] of Object.entries(panel._sessionTitleListeners)) {
    window.addEventListener(name, fn);
  }
}

export function detachSessionTitle(panel) {
  for (const [name, fn] of Object.entries(panel._sessionTitleListeners || {})) {
    window.removeEventListener(name, fn);
  }
  panel._sessionTitleListeners = null;
}

function beginEdit(panel) {
  panel._sessionTitleEditing = true;
  panel.updateComplete.then(() => {
    const input = panel.shadowRoot?.querySelector('.session-title-input');
    input?.focus();
    input?.select();
  });
}

/**
 * Rename the session on screen. An unchanged or empty title is a cancel:
 * the SDK cannot clear a title, and the CLI's would come back anyway.
 * The chip updates from the `session-labelled` broadcast, like every
 * other client's.
 */
async function commit(panel, rawValue) {
  const label = panel._sessionLabel;
  if (!label || panel._sessionTitlePending) return;
  const value = String(rawValue ?? '').trim();
  if (!value || value === label.title) {
    panel._sessionTitleEditing = false;
    return;
  }
  panel._sessionTitlePending = true;
  try {
    const result = await withRpcTimeout(
      panel.rpcExtract('ClaudeCodeService.history_rename', label.session_id, value),
      LABEL_TIMEOUT_MS,
      'history_rename',
    );
    if (result && result.error) {
      toast(String(result.error), 'warning');
      return;
    }
    panel._sessionTitleEditing = false;
  } catch (err) {
    console.error('[session-title] history_rename failed', err);
    toast(err?.message || 'Could not rename this session', 'warning');
  } finally {
    panel._sessionTitlePending = false;
  }
}

function toast(message, type) {
  window.dispatchEvent(
    new CustomEvent('aic-toast', { detail: { message, type }, bubbles: false }),
  );
}

function onKeyDown(panel, event) {
  if (event.key === 'Enter') {
    event.preventDefault();
    commit(panel, event.target.value);
  } else if (event.key === 'Escape') {
    event.preventDefault();
    event.stopPropagation();
    panel._sessionTitleEditing = false;
  }
}

/**
 * The chip, or nothing before the session has an ID — a session that does
 * not exist yet has no transcript to hold a title.
 */
export function renderSessionTitle(panel) {
  const label = panel._sessionLabel;
  if (!label?.session_id) return nothing;
  if (panel._sessionTitleEditing) {
    return html`<input
      class="session-title-input"
      type="text"
      .value=${label.title}
      placeholder="Session title"
      aria-label="Session title"
      ?disabled=${panel._sessionTitlePending}
      @keydown=${(e) => onKeyDown(panel, e)}
      @blur=${() => {
        if (!panel._sessionTitlePending) panel._sessionTitleEditing = false;
      }}
    />`;
  }
  const shown = label.title || 'Untitled session';
  return html`<button
    class="action-button session-title ${label.title ? '' : 'untitled'}"
    ?disabled=${!panel.rpcConnected}
    @click=${() => beginEdit(panel)}
    aria-label="Rename this session: ${shown}"
    title="${shown}${label.tag ? ` · ${label.tag}` : ''} — click to rename"
  >
    ${shown}${label.tag
      ? html`<span class="session-title-tag">${label.tag}</span>`
      : nothing}
  </button>`;
}
