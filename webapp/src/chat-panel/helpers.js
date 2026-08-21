// Pure helpers extracted from chat-panel.js.
//
// Module-scoped utilities and constants that don't touch
// component state. Safe to import from anywhere; no
// circular-import risk.
//
// Contents:
//   - generateRequestId: backend-compatible request ID
//   - _AGENT_LABEL_MAX_LENGTH: tab-strip label budget
//   - localStorage helpers for persisted toggles
//   - Scroll thresholds

/**
 * Generate a request ID matching the specs3 format so the
 * backend's correlation logic works unchanged. Format:
 * `{epoch_ms}-{6-char-alnum}`. Epoch gives monotonic ordering;
 * random suffix breaks ties on the same-millisecond case.
 */
export function generateRequestId() {
  const epoch = Date.now();
  const suffix = Math.random().toString(36).slice(2, 8);
  return `${epoch}-${suffix}`;
}

/**
 * Maximum visible width of a tab-strip label, in chars.
 *
 * Read now by ``tabs.js``'s subagent-transcript labels, which is the only
 * kind of tab left that names something the user did not type. Its
 * companion `deriveAgentTabLabel` — `Agent NN: {first line of task}`,
 * built from a `🟧🟧🟧 AGENT` block's index and task — went with the
 * protocol, as did `parseAgentTabId`, which mapped a tab id back to the
 * LLM-chosen agent id that every tagged RPC carried. Subagent tabs are
 * keyed by the `Task` call's `tool_use_id` and are not writable, so
 * there is no tagged call to address.
 */
export const _AGENT_LABEL_MAX_LENGTH = 40;

/** localStorage key for the snippet drawer's open/closed state. */
export const _DRAWER_STORAGE_KEY = 'aic-dc-snippet-drawer';

// The reasoning toggle's and effort selector's localStorage shims stood here
// until conversion phase 3. Both fed arguments to the native
// ``chat_streaming`` — ``reasoning`` and ``effort`` — that
// ``ClaudeCodeService.chat_streaming`` does not take: thinking depth is the
// CLI's to decide, and effort is set once in ``engine.json`` and consumed when
// the subprocess starts. The level list was also wrong for the new engine, on
// top of being unsent — it offered ``minimal``, which is not in the SDK's
// vocabulary (``low``/``medium``/``high``/``xhigh``/``max``), so a user's
// stored preference could not have been honoured even if something forwarded
// it.

export function _loadDrawerOpen() {
  try {
    return localStorage.getItem(_DRAWER_STORAGE_KEY) === 'true';
  } catch (_) {
    return false;
  }
}

export function _saveDrawerOpen(open) {
  try {
    localStorage.setItem(_DRAWER_STORAGE_KEY, open ? 'true' : 'false');
  } catch (_) {
    // Best-effort.
  }
}

/** Search toggle keys, kept compatible with specs3 history. */
export const _SEARCH_IGNORE_CASE_KEY = 'aic-dc-search-ignore-case';
export const _SEARCH_REGEX_KEY = 'aic-dc-search-regex';
export const _SEARCH_WHOLE_WORD_KEY = 'aic-dc-search-whole-word';

export function _loadSearchToggle(key, defaultValue) {
  try {
    const raw = localStorage.getItem(key);
    if (raw === 'true') return true;
    if (raw === 'false') return false;
    return defaultValue;
  } catch (_) {
    return defaultValue;
  }
}

export function _saveSearchToggle(key, value) {
  try {
    localStorage.setItem(key, value ? 'true' : 'false');
  } catch (_) {
    // Best-effort.
  }
}

/**
 * Format a run duration (milliseconds) for the assistant
 * run-timer display.
 *
 * The timer starts when the user's prompt is sent and stops
 * when the assistant response finishes; this turns the
 * elapsed millisecond count into a compact human-readable
 * string shown on the streaming card (live) and on the
 * settled assistant message (frozen).
 *
 * Buckets:
 *   - < 1 min   → seconds with one decimal ("4.2s"). The
 *                 decimal keeps the live counter visibly
 *                 moving for short turns without flickering
 *                 a hundredths digit.
 *   - < 1 hour  → "Mm SSs" ("1m 04s") — seconds zero-padded
 *                 so the width stays stable as it ticks.
 *   - >= 1 hour → "Hh MMm" ("2h 05m").
 *
 * Negative or non-finite inputs clamp to 0 so a clock skew
 * between start stamp and render never renders a negative
 * timer.
 */
export function formatRunDuration(ms) {
  const totalMs = Number.isFinite(ms) && ms > 0 ? ms : 0;
  const totalSeconds = totalMs / 1000;
  if (totalSeconds < 60) {
    return `${totalSeconds.toFixed(1)}s`;
  }
  if (totalSeconds < 3600) {
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = Math.floor(totalSeconds % 60);
    return `${minutes}m ${String(seconds).padStart(2, '0')}s`;
  }
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  return `${hours}h ${String(minutes).padStart(2, '0')}m`;
}

/**
 * How close to the bottom counts as "still at the bottom".
 */
export const AUTO_SCROLL_TOLERANCE_PX = 40;

/**
 * How far the user must scroll UP from the bottom to disengage
 * auto-scroll.
 */
export const AUTO_SCROLL_DISENGAGE_PX = 100;