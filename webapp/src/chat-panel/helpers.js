// Pure helpers extracted from chat-panel.js.
//
// Module-scoped utilities and constants that don't touch
// component state. Safe to import from anywhere; no
// circular-import risk.
//
// Contents:
//   - generateRequestId: backend-compatible request ID
//   - parseAgentTabId: tab ID → agent identifier
//   - deriveAgentTabLabel: tab strip label for an agent
//   - localStorage helpers for persisted toggles
//   - Scroll thresholds
//   - _EXPERIMENTAL_ENABLED gate

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

/** Maximum visible width of an agent tab label, in chars. */
export const _AGENT_LABEL_MAX_LENGTH = 40;

/**
 * Map a tab id to its backend agent identifier.
 *
 * Agent identity is the LLM-chosen id from the
 * ``🟧🟧🟧 AGENT`` block (e.g., ``"frontend-trivial"``).
 * Tab ids for agent tabs ARE that id directly; the
 * literal string ``"main"`` denotes the main
 * conversation.
 *
 * Returns the agent id (a non-empty string) for agent
 * tabs, or ``null`` for the main tab and for malformed
 * inputs. ``null`` tells the caller to omit the
 * ``agent_tag`` argument entirely (untagged call =
 * main conversation).
 *
 * @param {string} tabId — the tab's identifier
 * @returns {string | null}
 */
export function parseAgentTabId(tabId) {
  if (typeof tabId !== 'string' || !tabId) return null;
  if (tabId === 'main') return null;
  return tabId;
}

/**
 * Derive a tab-strip label for a spawned agent.
 *
 * Format: `Agent NN` for empty / whitespace tasks, or
 * `Agent NN: {first line of task}` for a populated task
 * — truncated to `_AGENT_LABEL_MAX_LENGTH` chars with a
 * trailing `…` when the task text doesn't fit.
 *
 * @param {number} agentIdx — zero-based agent index
 * @param {string | undefined | null} task — the agent's
 *   task text from the spawn block
 * @returns {string}
 */
export function deriveAgentTabLabel(agentIdx, task) {
  let idx = Number(agentIdx);
  if (!Number.isFinite(idx)) idx = 0;
  idx = Math.max(0, Math.floor(idx));
  const paddedIdx = String(idx).padStart(2, '0');
  const prefix = `Agent ${paddedIdx}`;

  if (typeof task !== 'string') return prefix;
  const firstLine = task
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find((line) => line.length > 0);
  if (!firstLine) return prefix;

  const full = `${prefix}: ${firstLine}`;
  if (full.length <= _AGENT_LABEL_MAX_LENGTH) return full;

  const keep = _AGENT_LABEL_MAX_LENGTH - 1;
  return `${full.slice(0, keep)}…`;
}

/**
 * Read the `?experimental=1` URL parameter set by the
 * Python launcher when started with `--experimental`.
 * Cached at module load so every chat-panel instance
 * sees the same value without re-parsing.
 */
export const _EXPERIMENTAL_ENABLED = (() => {
  try {
    const raw = new URLSearchParams(window.location.search).get(
      'experimental',
    );
    if (!raw) return false;
    return ['1', 'true', 'yes'].includes(raw.toLowerCase());
  } catch (_err) {
    return false;
  }
})();

/** localStorage key for the snippet drawer's open/closed state. */
export const _DRAWER_STORAGE_KEY = 'ac-dc-snippet-drawer';

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
export const _SEARCH_IGNORE_CASE_KEY = 'ac-dc-search-ignore-case';
export const _SEARCH_REGEX_KEY = 'ac-dc-search-regex';
export const _SEARCH_WHOLE_WORD_KEY = 'ac-dc-search-whole-word';

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