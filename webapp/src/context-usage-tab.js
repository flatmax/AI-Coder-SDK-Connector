// ContextUsageTab — what is in the engine's context window right now.
//
// Replaces `context-tab.js`, which had two sub-views (Budget and Cache)
// built entirely on AIC⚡DC's own prompt assembly: category allocations it
// chose, L0-L3 cache tiers it maintained, and a stability tracker that
// decided when a tier could graduate. None of that survives the
// conversion, and none of it had an analogue to port — the CLI
// assembles its own prompt and manages its own cache.
//
// So this tab answers the one question the old tab answered that still
// has an answer: what is filling the context window, and how much room
// is left. The data is `ClaudeCodeService.get_context_usage`, a
// pass-through of the same breakdown the CLI's `/context` command
// prints, so this view and that command cannot disagree.
//
// Governing spec: specs5/5-webapp/viewers-hud.md § Context Usage (CC-17).
//
// What it deliberately does NOT do:
//   - No refresh loop. The breakdown only changes when a turn runs or a
//     session loads, so it refreshes on exactly those events plus an
//     explicit button. A poll would spend control requests to watch a
//     number that cannot move on its own.
//   - No "rebuild cache" affordance. The cache is the CLI's, and it has
//     no request to rebuild it. A button that quietly did nothing would
//     be worse than no button.

import { LitElement, css, html } from 'lit';
import { RpcMixin } from './rpc-mixin.js';
import { withRpcTimeout } from './rpc.js';
import {
  bandColor as _pctColor,
  categoryColor,
  compactionLimit,
  compactionPercent,
  mcpHealth,
  messageComposition,
  overLimit,
  partitionCategories,
  serverGroups,
  skillInventory,
  sourceLabel,
  thresholdPercent,
  warningPercent,
  windowPercent,
} from './context-usage.js';
import { toRepoPath } from './repo-path.js';
import {
  SURFACE,
  loadCapabilities,
  supports,
} from './engine-capabilities.js';
// The rate-limit derivations are shared with the HUD and the chat panel's
// toast rather than reimplemented: `windowIsOpen` in particular is the single
// definition of "has this window reset yet", which the server deliberately
// does not answer (`specs5/next.md` § C3).
import {
  formatResetTime,
  limitTypeLabel,
  utilizationPercent,
  windowIsOpen,
} from './rate-limit.js';
// `formatCost` is the CLI's own format, and `usageSplit` reads either
// spelling of the counters. Both shared with the HUD for the same reason.
import { formatCost, usageSplit } from './turn-cost.js';

/**
 * Deadline for a breakdown fetch. Without one, a reply dropped by a
 * reconnecting socket leaves `_loading` set — which both blocks every
 * later refresh and disables the Refresh button that would retry.
 *
 * Deliberately *above* the SDK's own 60s control-request deadline, not
 * below it. `ClaudeCodeService.get_context_usage` catches that timeout
 * and answers `{error}`, so the backend always replies; a shorter
 * deadline here would pre-empt a reply that is on its way and stack a
 * retry onto a subprocess already struggling to answer the first. This
 * call is slow — measured live at 3-5s warm, 14s on the first fetch
 * after an idle session, and past 60s often enough to log eight
 * `Control request timeout: get_context_usage` failures in one
 * half-hour run. So the only case left for this deadline is the one it
 * was written for: no reply is coming at all.
 */
const _FETCH_TIMEOUT_MS = 90000;

/**
 * The three sections `viewers-hud.md` gives this tab.
 *
 * Debug was absent until its four sources had a reader — a segment that
 * opens onto nothing is the disclosure-triangle problem the categories
 * table already refuses to have. It has them now: `hookEvent` traffic
 * arrives pushed, `get_engine_health` and `get_server_info` are fetched
 * on the way in, `get_mcp_status` is already here for the Session
 * section's pills, and `gridRows` rides along on the breakdown.
 *
 * Usage is the default and Debug is never it, which is what "off by
 * default" means for a section behind a segmented control. A reader who
 * chooses it still gets it back on the next visit, like the other two.
 */
const _SECTIONS = [
  { id: 'usage', label: 'Usage' },
  { id: 'session', label: 'Session' },
  { id: 'debug', label: 'Debug' },
];

/** localStorage key for the section the user was last reading. */
const _SECTION_KEY = 'aic-dc-context-section';

function _loadSection() {
  try {
    if (typeof localStorage === 'undefined') return 'usage';
    const saved = localStorage.getItem(_SECTION_KEY);
    return _SECTIONS.some((s) => s.id === saved) ? saved : 'usage';
  } catch {
    return 'usage';
  }
}

function _saveSection(id) {
  try {
    if (typeof localStorage === 'undefined') return;
    localStorage.setItem(_SECTION_KEY, id);
  } catch {
    // A private-mode quota error is not worth surfacing; the section
    // simply does not persist.
  }
}

/**
 * A tool group's tokens, split by whether the window is paying for them.
 *
 * Never summed, here or anywhere the Tools section reports a figure: a
 * deferred tool's schema is not loaded until first use, so one number
 * covering both would report a cost the session is not paying.
 */
function _splitTokens(group) {
  if (group.server) {
    return {
      loaded: group.server.loadedTokens,
      deferred: group.server.deferredTokens,
    };
  }
  let loaded = 0;
  let deferred = 0;
  for (const t of group.tools) {
    if (t.deferred) deferred += t.tokens;
    else loaded += t.tokens;
  }
  return { loaded, deferred };
}

/** The token clauses a tool heading carries, in one phrasing. */
function _tokenBits(loaded, deferred) {
  const bits = [];
  if (loaded > 0) bits.push(`${_fmtTokens(loaded)} loaded`);
  if (deferred > 0) bits.push(`${_fmtTokens(deferred)} deferred`);
  if (bits.length === 0) bits.push('no tokens');
  return bits;
}

function _fmtTokens(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  if (v < 1000) return String(Math.round(v));
  if (v < 1_000_000) return `${(v / 1000).toFixed(1)}K`;
  return `${(v / 1_000_000).toFixed(2)}M`;
}

/**
 * Fold one rate-limit record into a list, keyed by window.
 *
 * Keyed rather than appended because the CLI re-sends a window on every
 * transition: a list that grew per event would draw the five-hour window
 * three times, at three different utilisations, all of them claiming to be
 * current. An untyped record gets a placeholder key rather than being
 * dropped — the enum is the CLI's to extend, and a figure under a made-up
 * key is more use than no figure.
 *
 * Returns a new array, because Lit's default `hasChanged` is identity.
 */
function _mergeRateLimit(records, record) {
  if (!record || typeof record !== 'object') return records;
  const key = record.rate_limit_type || '_untyped';
  const kept = (records || []).filter(
    (r) => (r?.rate_limit_type || '_untyped') !== key,
  );
  return [...kept, record];
}

/**
 * The session's wall duration, aged from when the snapshot was taken.
 *
 * The server measures on a monotonic clock and sends a *number of seconds*,
 * not an instant, so a figure rendered straight from the snapshot is frozen
 * at whenever that snapshot happened — which on a long session is the one
 * moment it is certainly wrong about. Ageing it here is the same rule the
 * rate-limit windows follow: the server serves the record raw and the
 * browser reads it against its own clock.
 *
 * No timer drives this. It re-reads on every render, which is every turn and
 * every tab entry — often enough for a figure nobody watches tick.
 */
function _sessionSeconds(usage) {
  const base = Number(usage?.duration_seconds);
  if (!Number.isFinite(base) || base < 0) return null;
  const takenAt = Number(usage?._takenAt);
  if (!Number.isFinite(takenAt)) return base;
  return base + Math.max(0, (Date.now() - takenAt) / 1000);
}

/** A duration in the CLI's own shape: `0s`, `3s`, `4m 12s`, `2h 5m`. */
function _fmtDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '—';
  const total = Math.floor(seconds);
  if (total < 60) return `${total}s`;
  if (total < 3600) return `${Math.floor(total / 60)}m ${total % 60}s`;
  return `${Math.floor(total / 3600)}h ${Math.floor((total % 3600) / 60)}m`;
}

/**
 * A recorded failure's ISO timestamp, in the reader's own time.
 *
 * Absolute rather than relative ("3 hours ago"), which is the opposite of
 * how the history browser stamps a session. The question here is not how
 * long ago it happened but whether it lines up with something the user
 * remembers doing — opening a terminal, a laptop waking, a token expiring —
 * and a relative stamp cannot be matched against a memory of a clock.
 *
 * Unparseable text is shown verbatim rather than swallowed: it came out of
 * a file, and a reader who can see the raw string can tell a corrupt record
 * from a missing one.
 */
function _engineErrorTime(iso) {
  if (!iso) return '—';
  const when = new Date(iso);
  return Number.isNaN(when.getTime()) ? String(iso) : when.toLocaleString();
}

/**
 * How many hook events the Debug section keeps.
 *
 * A ring buffer rather than the session's whole traffic: the `PostToolUse`
 * re-index fires on every file the agent writes, so one long turn can
 * produce hundreds. "What just fired" is the question a hook log answers,
 * and the newest few are the answer.
 */
const _HOOK_LOG_LIMIT = 40;

/** Longest raw payload the Debug section prints, in characters. */
const _JSON_LIMIT = 20000;

/**
 * A raw payload, formatted for reading and bounded.
 *
 * The bound is not cosmetic: `gridRows` and a server-info reply are both
 * unbounded in principle, and a 200 KB `<pre>` in a shadow root is a
 * scroll container the panel never recovers from.
 */
function _json(value) {
  let text;
  try {
    text = JSON.stringify(value, null, 2);
  } catch {
    // Circular structures are the only realistic cause here, and a
    // debug view that throws on one is worse than one that says so.
    return '(not serialisable)';
  }
  if (typeof text !== 'string') return String(text);
  if (text.length <= _JSON_LIMIT) return text;
  return `${text.slice(0, _JSON_LIMIT)}\n… truncated at ${_JSON_LIMIT} characters.`;
}

/**
 * One line describing a value, without knowing what it is.
 *
 * The Debug section summarises `get_server_info()` by key rather than by
 * field name on purpose. Its shape is the CLI's initialize reply, this
 * repo has not read a schema for it, and naming fields we have not
 * verified is the exact mistake `3-engine/context-visibility.md` records
 * under *Verified field shapes*. So the summary counts what is there and
 * the dump below it carries the truth.
 */
function _describeValue(v) {
  if (Array.isArray(v)) {
    return `${v.length} ${v.length === 1 ? 'entry' : 'entries'}`;
  }
  if (v === null || v === undefined) return '—';
  if (typeof v === 'object') {
    const n = Object.keys(v).length;
    return `${n} ${n === 1 ? 'key' : 'keys'}`;
  }
  const text = String(v);
  return text.length > 80 ? `${text.slice(0, 80)}…` : text;
}

export class ContextUsageTab extends RpcMixin(LitElement) {
  static properties = {
    /**
     * The SDK's `ContextUsageResponse`, or null before the first
     * successful fetch.
     */
    _usage: { type: Object, state: true },
    /** When the engine's answer was taken, as an ISO string. */
    _fetchedAt: { type: String, state: true },
    /** Error text from the last failed fetch, or ''. */
    _error: { type: String, state: true },
    /** True while a fetch is outstanding. */
    _loading: { type: Boolean, state: true },
    /**
     * True when a turn completed while this tab was hidden, so the
     * numbers on screen predate it. Shown as a badge rather than
     * silently refreshed, because the user may be mid-read.
     */
    _stale: { type: Boolean, state: true },
    /** Which section is on screen: one of `_SECTIONS`'s ids. */
    _section: { type: String, state: true },
    /**
     * What this session has spent: `{total_cost_usd, model_usage,
     * duration_seconds}`, or null before the first snapshot.
     *
     * The **cumulative** figures, deliberately, and the only place in this
     * app that reads them as such. Everywhere else `total_cost_usd` and
     * `model_usage` rendered as a turn's are the bug `turn-cost.js` exists
     * to prevent; the question this block answers is the one they actually
     * answer. Labelled "Session" on screen for the same reason.
     */
    _sessionUsage: { type: Object, state: true },
    /**
     * Every rate-limit window the account has open, newest arrival last.
     *
     * An array rather than the HUD's single record, because an account has
     * several at once — the CLI's own `/usage` draws a gauge for the
     * five-hour window, one for the week across all models, and one per
     * model with its own cap. Merged by `rate_limit_type` from two arrivals
     * (the snapshot and the live push) the same way the HUD merges its one.
     */
    _rateLimits: { type: Array, state: true },
    /**
     * `ClaudeCodeService.get_account_usage` — the windows the CLI's own
     * `/usage` draws, read from the account rather than from the engine.
     *
     * **This is the figures; `_rateLimits` above is the alarms.** The
     * engine's channel emits on a transition and mostly carries no
     * utilisation at all, so it can say *that something changed* and not
     * *where you stand*; the server reads the same REST endpoint the CLI
     * reads to answer the second question. Held separately rather than
     * merged into `_rateLimits` because the two disagree about what a
     * record even is, and because this one is available with **no engine
     * running** — which is the state a rate-limited reader is in.
     *
     * Shape: `{ok, windows[], fetched_at}` or `{ok: false, reason, detail}`.
     * Null before the first fetch answers.
     */
    _accountUsage: { type: Object, state: true },
    /**
     * The SDK's `McpStatusResponse` from the last successful fetch, or
     * null. Separate from `_usage` because it comes from a second call
     * that is allowed to fail on its own.
     */
    _mcpStatus: { type: Object, state: true },
    /**
     * False once `Collab.get_collab_role` reports this client is not the
     * host.
     *
     * `reconnect_mcp_server` and `toggle_mcp_server` are both
     * localhost-only, so a guest reads the connection facts without the
     * actions on them. Same narrowing as the model selector's
     * `_canSetModel`, and for the same reason: the RPCs run
     * `_check_localhost_only` themselves, so being wrong here costs a
     * rejected call rather than an unauthorised one.
     */
    _canControlMcp: { type: Boolean, state: true },
    /**
     * Which kind of failure `_error` was: 'no-engine' when there is no
     * session to ask, 'failed' when a request went out and did not come
     * back with an answer. Empty when nothing has failed.
     *
     * Only the advice under the error changes, and it changes to the
     * opposite advice — wait for a session, or retry the one you have.
     * The service names it rather than the viewer guessing from the
     * error string, which would go wrong the first time either side
     * reworded anything.
     */
    _errorReason: { type: String, state: true },
  };

  static styles = css`
    :host {
      display: flex;
      flex-direction: column;
      height: 100%;
      min-height: 0;
      background: var(--bg-primary, #0d1117);
      color: var(--text-primary, #c9d1d9);
      font-size: 0.875rem;
      overflow-y: auto;
    }

    .toolbar {
      flex-shrink: 0;
      display: flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.5rem 1rem;
      border-bottom: 1px solid rgba(240, 246, 252, 0.1);
      background: rgba(22, 27, 34, 0.4);
    }
    .back-btn,
    .tool-btn {
      background: transparent;
      border: 1px solid rgba(240, 246, 252, 0.15);
      color: var(--text-secondary, #8b949e);
      padding: 0.2rem 0.5rem;
      border-radius: 4px;
      cursor: pointer;
      font-size: 0.875rem;
      line-height: 1;
      font-family: inherit;
    }
    .back-btn:hover,
    .tool-btn:hover:not(:disabled) {
      background: rgba(240, 246, 252, 0.06);
      color: var(--text-primary, #c9d1d9);
      border-color: rgba(240, 246, 252, 0.3);
    }
    .tool-btn:disabled {
      opacity: 0.5;
      cursor: default;
    }
    .spacer {
      flex: 1 1 auto;
    }
    .stale-badge {
      color: #d29922;
      font-size: 0.75rem;
    }

    .segmented {
      flex-shrink: 0;
      display: flex;
      gap: 0.25rem;
      padding: 0.5rem 1rem 0;
    }
    .seg {
      background: transparent;
      border: 1px solid rgba(240, 246, 252, 0.15);
      color: var(--text-secondary, #8b949e);
      padding: 0.2rem 0.7rem;
      border-radius: 999px;
      cursor: pointer;
      font-size: 0.75rem;
      font-family: inherit;
      line-height: 1.4;
    }
    .seg:hover {
      background: rgba(240, 246, 252, 0.06);
      color: var(--text-primary, #c9d1d9);
    }
    .seg[aria-selected='true'] {
      background: rgba(88, 166, 255, 0.15);
      border-color: rgba(88, 166, 255, 0.5);
      color: var(--text-primary, #c9d1d9);
    }

    .content {
      padding: 1rem;
      display: flex;
      flex-direction: column;
      gap: 1.25rem;
    }

    .headline {
      display: flex;
      align-items: baseline;
      gap: 0.5rem;
    }
    .headline .pct {
      font-size: 1.5rem;
      font-weight: 600;
      font-variant-numeric: tabular-nums;
    }
    .headline .of {
      color: var(--text-secondary, #8b949e);
      font-variant-numeric: tabular-nums;
    }
    .model-note {
      color: var(--text-secondary, #8b949e);
      font-size: 0.75rem;
    }

    .bar {
      position: relative;
      height: 12px;
      border-radius: 6px;
      background: rgba(240, 246, 252, 0.08);
      overflow: hidden;
      display: flex;
    }
    .bar.gauge {
      height: 18px;
    }
    /**
     * One rate-limit window. Several stack, and without the gap a run of
     * them reads as one gauge with several fills — which is the misreading
     * the single-slot record used to force on the HUD.
     */
    .limit {
      margin-bottom: 0.7rem;
    }
    .limit:last-child {
      margin-bottom: 0;
    }
    .limit .note {
      margin: 0.2rem 0 0;
    }
    .bar-seg {
      height: 100%;
    }
    /**
     * The mark's positioning parent, and the reason it is not the bar.
     *
     * .bar clips its children — that is what rounds the ends of the
     * segment fill — so a mark inside it loses precisely the pixel of
     * overhang and the ring below that make it legible against a
     * segment of any colour. Sibling of the bar, positioned over it.
     */
    .bar-wrap {
      position: relative;
    }
    /**
     * The autocompact mark. Over the bar rather than a tick beneath
     * it, so the fill is read against it directly — the question is
     * "have the segments reached the mark", and an aligned tick two
     * pixels below makes that a comparison instead of a glance.
     */
    .mark {
      position: absolute;
      top: -1px;
      bottom: -1px;
      width: 2px;
      margin-left: -1px;
      background: var(--text-primary, #c9d1d9);
      box-shadow: 0 0 0 1px rgba(13, 17, 23, 0.8);
      pointer-events: none;
    }
    .mark-note {
      display: flex;
      justify-content: space-between;
      color: var(--text-secondary, #8b949e);
      font-size: 0.6875rem;
      font-variant-numeric: tabular-nums;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-variant-numeric: tabular-nums;
    }
    th {
      text-align: left;
      font-weight: 500;
      color: var(--text-secondary, #8b949e);
      font-size: 0.75rem;
      padding: 0.25rem 0.5rem 0.25rem 0;
      border-bottom: 1px solid rgba(240, 246, 252, 0.1);
    }
    th.num,
    td.num {
      text-align: right;
      padding-right: 0;
    }
    td {
      padding: 0.25rem 0.5rem 0.25rem 0;
      border-bottom: 1px solid rgba(240, 246, 252, 0.05);
    }
    tr.deferred td {
      opacity: 0.6;
      font-style: italic;
    }
    .swatch {
      display: inline-block;
      width: 9px;
      height: 9px;
      border-radius: 2px;
      margin-right: 0.4rem;
      vertical-align: middle;
    }
    .path {
      font-family: var(--font-mono, ui-monospace, monospace);
      font-size: 0.75rem;
      word-break: break-all;
    }
    /**
     * A table cell that opens something. A button rather than a styled
     * span so it is reachable by keyboard and announced as an action —
     * the row looks like text either way.
     */
    .link {
      background: none;
      border: none;
      padding: 0;
      margin: 0;
      font: inherit;
      color: var(--accent, #58a6ff);
      cursor: pointer;
      text-align: left;
      word-break: break-all;
    }
    .link:hover {
      text-decoration: underline;
    }

    .group {
      display: flex;
      flex-direction: column;
      gap: 0.3rem;
      border-bottom: 1px solid rgba(240, 246, 252, 0.05);
      padding-bottom: 0.3rem;
    }
    .group-head {
      display: flex;
      align-items: center;
      gap: 0.4rem;
      width: 100%;
      background: none;
      border: none;
      padding: 0.3rem 0;
      color: var(--text-primary, #c9d1d9);
      font: inherit;
      cursor: pointer;
      text-align: left;
    }
    .group-head:hover {
      color: #fff;
    }
    .chev {
      color: var(--text-secondary, #8b949e);
      font-size: 0.7rem;
      width: 0.7rem;
      flex-shrink: 0;
    }
    .group-name {
      font-weight: 500;
    }
    .group-meta {
      color: var(--text-secondary, #8b949e);
      font-size: 0.75rem;
      font-variant-numeric: tabular-nums;
      flex-shrink: 0;
    }
    .pill {
      border: 1px solid currentColor;
      border-radius: 999px;
      padding: 0 0.4rem;
      font-size: 0.6875rem;
      line-height: 1.5;
      white-space: nowrap;
    }

    h3 {
      margin: 0;
      font-size: 0.8125rem;
      font-weight: 600;
      color: var(--text-secondary, #8b949e);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    h3 .sub {
      text-transform: none;
      letter-spacing: 0;
      font-weight: 400;
      font-variant-numeric: tabular-nums;
    }
    section {
      display: flex;
      flex-direction: column;
      gap: 0.4rem;
    }
    .empty,
    .note {
      color: var(--text-secondary, #8b949e);
      font-size: 0.8125rem;
    }
    .actions {
      display: flex;
      align-items: center;
      gap: 0.4rem;
      margin: 0.1rem 0 0;
    }
    .acting {
      color: var(--text-secondary, #8b949e);
      font-size: 0.8125rem;
    }
    .error {
      color: #f85149;
    }
    .warn {
      color: #d29922;
    }

    /**
     * A raw payload. Capped in height rather than left to grow: the grid
     * rows alone are longer than this panel, and a dump that pushes the
     * sections under it off screen makes the Debug section unreadable in
     * the one situation it exists for.
     */
    h4 {
      margin: 0.8rem 0 0.3rem;
      font-size: 0.8125rem;
      color: var(--text-primary, #e6edf3);
    }
    /**
     * One recorded engine failure. Ruled off on the left rather than
     * boxed, because several of these stack and a run of boxes reads as a
     * list of unrelated things when it is usually one fault repeating.
     */
    .engine-error {
      margin: 0 0 0.6rem;
      padding-left: 0.6rem;
      border-left: 2px solid rgba(248, 81, 73, 0.4);
    }
    .engine-error p.error {
      margin: 0 0 0.3rem;
    }
    /**
     * The stderr dump shares this rule rather than getting its own: it is
     * the same decision — a raw payload capped in height so it cannot push
     * the sections under it off screen — applied to text that happens not
     * to be JSON. Two rules would be one rule that could drift.
     *
     * (No backticks anywhere in this block: the whole of the styles is one
     * template literal, and a stray backtick in a comment ends it. The
     * failure is a parse error hundreds of lines away with no line number.)
     */
    pre.json,
    pre.stderr {
      margin: 0;
      max-height: 16rem;
      overflow: auto;
      padding: 0.5rem;
      background: rgba(240, 246, 252, 0.04);
      border: 1px solid rgba(240, 246, 252, 0.08);
      border-radius: 4px;
      font-family: var(--font-mono, ui-monospace, monospace);
      font-size: 0.6875rem;
      line-height: 1.45;
      color: var(--text-secondary, #8b949e);
    }
  `;

  constructor() {
    super();
    this._usage = null;
    this._fetchedAt = '';
    this._error = '';
    this._errorReason = '';
    this._loading = false;
    this._stale = false;
    this._section = _loadSection();
    this._mcpStatus = null;
    this._canControlMcp = true;
    /**
     * Server names with a control request in flight, so one row's buttons
     * go quiet without disabling every other row's. A plain Set on the
     * same grounds as `_openGroups` below — mutated in place, which Lit's
     * identity check never sees, so each mutator asks for the render.
     */
    this._mcpBusy = new Set();
    /**
     * Which tool groups are expanded, by group key. A plain Set rather
     * than a reactive property: Lit compares by identity and mutating a
     * Set in place never trips that, so `_toggleGroup` asks for the
     * render itself. Collapsed is the default because the header carries
     * the counts — the summary is the thing most readers want, and 35
     * `aic-dc` rows expanded on arrival bury every other section.
     */
    this._openGroups = new Set();

    /**
     * Debug's state, deliberately *not* reactive properties.
     *
     * Hook events arrive throughout a turn — dozens of them, since every
     * file the agent writes fires `PostToolUse` — and a reactive field
     * would re-render this whole panel for each one while the reader is
     * looking at Usage, or at another tab entirely. So these are plain
     * fields and `_debugUpdate()` asks for the render only when Debug is
     * the section on screen. Same rule as `_openGroups`, for the same
     * reason: Lit's identity check is not the right trigger here.
     */
    this._hooks = [];
    this._hookSeq = 0;
    /** The last `engineHealth` record, pushed or fetched. */
    this._health = null;
    /**
     * How many pushes have landed. Read either side of the health *fetch*
     * so a push that arrives mid-flight is not overwritten by the older
     * snapshot the server was already composing — see `_ensureDebug`.
     */
    this._healthSeq = 0;
    /** `get_server_info()`'s reply, fetched when Debug is first opened. */
    this._serverInfo = null;
    /**
     * Recent engine failures, or null before the first read.
     *
     * The one thing in this section that outlives the process it describes.
     * Everything else here is what the *running* engine resolved; this is
     * why an earlier one did not run, read back off disk, which is the only
     * way a failure the user has already closed the terminal on can still
     * be diagnosed.
     */
    this._engineErrors = null;
    this._debugError = '';
    this._debugLoading = false;

    this._sessionUsage = null;
    this._rateLimits = [];
    this._accountUsage = null;

    this._onStreamComplete = this._onStreamComplete.bind(this);
    this._onSessionChanged = this._onSessionChanged.bind(this);
    this._onHookEvent = this._onHookEvent.bind(this);
    this._onEngineHealth = this._onEngineHealth.bind(this);
    this._onRateLimit = this._onRateLimit.bind(this);
    this._onStateLoaded = this._onStateLoaded.bind(this);
  }

  connectedCallback() {
    super.connectedCallback();
    window.addEventListener('stream-complete', this._onStreamComplete);
    window.addEventListener('session-changed', this._onSessionChanged);
    // Both of these are pushes, not fetches, and both are collected even
    // while this panel is hidden: a hook log that starts empty when Debug
    // is opened has no history to show, and the interesting traffic
    // happened during the turn the reader is now asking about.
    window.addEventListener('hook-event', this._onHookEvent);
    window.addEventListener('engine-health', this._onEngineHealth);
    // The same two arrivals the HUD reads, for the same reason: the push is
    // the only thing that reports a *transition*, and the snapshot is the
    // only thing a browser that reloaded between transitions will ever get.
    // This section outlives the HUD's 8.8 seconds, which is the whole point
    // of it being here — `/usage` opens onto this tab.
    window.addEventListener('rate-limit', this._onRateLimit);
    window.addEventListener('state-loaded', this._onStateLoaded);
  }

  disconnectedCallback() {
    window.removeEventListener('stream-complete', this._onStreamComplete);
    window.removeEventListener('session-changed', this._onSessionChanged);
    window.removeEventListener('hook-event', this._onHookEvent);
    window.removeEventListener('engine-health', this._onEngineHealth);
    window.removeEventListener('rate-limit', this._onRateLimit);
    window.removeEventListener('state-loaded', this._onStateLoaded);
    super.disconnectedCallback();
  }

  /**
   * One window's record arrived. Merge it by type; never clear on nothing.
   *
   * Merged rather than appended, because the CLI re-sends a window on every
   * transition and a list that grew per event would draw the same window
   * three times with three different utilisations.
   */
  _onRateLimit(event) {
    const record = event?.detail?.data;
    if (!record || typeof record !== 'object') return;
    this._rateLimits = _mergeRateLimit(this._rateLimits, record);
  }

  /**
   * The shell's first-paint snapshot: every open window, and the session's
   * own totals.
   *
   * A snapshot carrying neither leaves both alone rather than clearing them.
   * A reconnect re-delivers the snapshot mid-session and a backend older
   * than these fields sends nothing at all; neither is evidence that a limit
   * the browser has been told about has gone away.
   */
  _onStateLoaded(event) {
    const detail = event?.detail;
    if (!detail || typeof detail !== 'object') return;
    if (Array.isArray(detail.rate_limits) && detail.rate_limits.length) {
      this._rateLimits = detail.rate_limits.reduce(_mergeRateLimit, this._rateLimits);
    }
    if (detail.session_usage && typeof detail.session_usage === 'object') {
      // Stamped on arrival so the duration can be aged at render time. The
      // server measures wall time on a monotonic clock and sends a number,
      // not an instant; without the stamp the figure would be frozen at
      // whenever the snapshot happened to be taken, which on a long session
      // is the one moment it is guaranteed to be wrong about.
      this._sessionUsage = { ...detail.session_usage, _takenAt: Date.now() };
    }
  }

  async onRpcReady() {
    // Not awaited alongside the refresh: the probe is a cheap read on
    // another service, the breakdown is a 3-14s control request, and what
    // the probe gates sits inside a collapsed group the reader has to open
    // before it is on screen at all.
    this._probeMcpAuthority();
    // Fired, not awaited — the same shape the HUD uses, and for the same
    // reason. Putting a round trip in front of the breakdown would delay
    // the thing this tab exists for in order to save two calls that fail
    // loudly when they are wrong. `supports()` answers "yes" until the
    // descriptor lands, so the first refresh may spend those two calls
    // once; the re-render is what takes the panels away afterwards.
    loadCapabilities(this).then(() => this.requestUpdate());
    await this._refresh();
  }

  /** Called by the dialog when this tab becomes visible. */
  onTabVisible() {
    if (this._stale || !this._usage) {
      this._stale = false;
      this._refresh();
    }
  }

  // ---------------------------------------------------------------
  // Events
  // ---------------------------------------------------------------

  _isTabActive() {
    const panel = this.parentElement;
    if (panel?.classList?.contains('tab-panel')) {
      return panel.classList.contains('active');
    }
    return this.offsetParent !== null;
  }

  _onStreamComplete(event) {
    // The session's running totals ride in on every result, so this block
    // stays current without a fetch — unlike the breakdown below it. Adopted
    // even while the tab is hidden, because it costs nothing and the
    // alternative is a stale figure the moment the tab is opened.
    //
    // `total_cost_usd` and `model_usage` **as reported**, which is the one
    // reading of them this app permits (`turn-cost.js` § the module note).
    // A result that carries neither leaves the last good figures alone: a
    // synthetic failure footer writes `null` into both, and a session does
    // not stop having cost what it cost because one turn died.
    const result = event?.detail;
    if (result && typeof result === 'object') {
      const cost = typeof result.total_cost_usd === 'number'
        ? result.total_cost_usd
        : this._sessionUsage?.total_cost_usd ?? null;
      const models = result.model_usage && typeof result.model_usage === 'object'
        ? result.model_usage
        : this._sessionUsage?.model_usage ?? null;
      this._sessionUsage = {
        ...(this._sessionUsage || {}),
        total_cost_usd: cost,
        model_usage: models,
      };
    }
    // Only fetch when the user can see the result. Unlike the old tab,
    // which fetched eagerly on every completion, this costs a control
    // request to the CLI subprocess rather than a local computation —
    // so a hidden tab marks itself stale and refreshes on the way in.
    if (this._isTabActive()) this._refresh();
    else this._stale = true;
  }

  _onSessionChanged() {
    // A resume or a fork is a different session, and both of Debug's
    // fetched answers belong to the old one: the advertised commands come
    // from an initialize this session did not do, and the hook traffic
    // records tool calls in a conversation the user has left. Dropped
    // rather than carried over, on the same principle as the health pill.
    this._serverInfo = null;
    this._debugError = '';
    this._hooks = [];
    if (this._isTabActive()) this._refresh();
    else this._stale = true;
  }

  /**
   * A hook fired. Kept newest-first in a bounded log.
   *
   * The id is a sequence number rather than the array index, because the
   * expanded-row state is keyed by it and an index shifts under the reader
   * every time a new hook arrives.
   */
  _onHookEvent(e) {
    const data = e?.detail?.data;
    if (!data || typeof data !== 'object') return;
    this._hookSeq += 1;
    const entry = {
      id: this._hookSeq,
      at: Date.now(),
      name: String(data.hook_event_name || data.phase || 'hook'),
      phase: data.phase ? String(data.phase) : '',
      tool: data.tool_name ? String(data.tool_name) : '',
      outcome: data.outcome != null && data.outcome !== '' ? String(data.outcome) : '',
      exitCode: Number.isFinite(Number(data.exit_code)) ? Number(data.exit_code) : null,
      raw: data.raw && typeof data.raw === 'object' ? data.raw : data,
    };
    this._hooks = [entry, ...this._hooks].slice(0, _HOOK_LOG_LIMIT);
    this._debugUpdate();
  }

  /**
   * The engine's own health record, which arrives pushed on connect and
   * again whenever it changes — a mirror gap, a lost session.
   *
   * Preferred over the fetch for exactly that reason: `mirror_gaps` moves
   * during a turn, and a Debug section showing the count from when it was
   * opened would report a clean mirror over a broken one.
   */
  _onEngineHealth(e) {
    const data = e?.detail;
    if (!data || typeof data !== 'object') return;
    this._health = data;
    this._healthSeq += 1;
    this._debugUpdate();
  }

  /**
   * Render, but only for a reader who is looking at Debug.
   *
   * Everything this guards is diagnostic state that changes far more often
   * than the breakdown does.
   */
  _debugUpdate() {
    if (this._section === 'debug') this.requestUpdate();
  }

  // ---------------------------------------------------------------
  // Data
  // ---------------------------------------------------------------

  /**
   * @param {{force?: boolean}} opts — `force` bypasses the server's cache
   *   for the account windows. Set by the Refresh button and by nothing
   *   else: a tab entry or a finished turn wants *current enough*, while
   *   somebody pressing Refresh is asking a question the cached answer has
   *   already failed to settle.
   */
  async _refresh({ force = false } = {}) {
    if (this._loading) return;
    if (!this.rpcConnected) return;
    this._loading = true;
    try {
      // Started together and awaited together, but they are **not** the
      // same kind of call and neither one's failure may take the other
      // down: the breakdown is a control request to a running engine, and
      // the account windows are a REST read that answers whether or not
      // one exists. `_fetchAccountUsage` swallows its own errors for that
      // reason, so a `Promise.all` here cannot reject on its account.
      await Promise.all([
        this._refreshBreakdown(),
        this._fetchAccountUsage({ force }),
      ]);
    } finally {
      this._loading = false;
    }
    // Refresh means "refresh what I am reading", and for a reader on Debug
    // that is the engine's own answers. Outside the breakdown's guard so a
    // *failed* breakdown — the case Debug exists to explain — refreshes
    // them too, and `_ensureDebug` has its own in-flight flag.
    if (this._section === 'debug') await this._ensureDebug({ force: true });
  }

  /** The breakdown and its health decoration; errors land in `_error`. */
  async _refreshBreakdown() {
    try {
      // Both calls go out together: they are separate control requests
      // to the same subprocess, and this one is slow enough (3-14s) that
      // sequencing them would put the health pill a full breakdown
      // behind the numbers it annotates.
      const [res, status] = await Promise.all([
        withRpcTimeout(
          this.rpcExtract('ClaudeCodeService.get_context_usage'),
          _FETCH_TIMEOUT_MS,
          'get_context_usage',
        ),
        this._fetchMcpStatus(),
      ]);
      // Written even when the breakdown failed, and written as null on
      // its own failure: a "connected" pill kept from an earlier fetch
      // is a claim about now, and this is the one field where being
      // out of date is worse than being absent.
      this._mcpStatus = status;
      if (res && res.error) {
        this._error = String(res.error);
        // A backend older than the reason field leaves this empty, and
        // the note falls back to the general advice rather than picking
        // one of the two specific ones on a guess.
        this._errorReason = res.reason ? String(res.reason) : '';
        return;
      }
      const usage = res && res.usage ? res.usage : null;
      if (!usage) {
        this._error = 'The engine returned no context usage.';
        // An answer arrived, so a session answered — 'failed' is the
        // honest half of what we know.
        this._errorReason = 'failed';
        return;
      }
      this._usage = usage;
      this._fetchedAt = res.fetched_at || '';
      this._error = '';
      this._errorReason = '';
      this._stale = false;
    } catch (err) {
      this._error = err?.message || 'Could not read context usage.';
      // The request left and did not come back — a timeout, or a socket
      // that dropped under it. Either way a request failed, which is
      // what 'failed' claims; whether an engine is up it cannot say.
      this._errorReason = 'failed';
    }
  }

  /**
   * The account's rate-limit windows, fetched beside the breakdown.
   *
   * Quiet on failure like `_fetchMcpStatus`, and for a stronger reason:
   * the server already turns every failure it can name into an
   * `{ok: false, reason}` answer, so anything thrown here is the *call*
   * failing — a timeout, a dropped socket, or a backend too old to have
   * the method. None of those is worth an error paragraph over a page of
   * working numbers, and the last good answer is better company for the
   * reader than a blank section.
   *
   * **The previous answer is kept on a thrown call, not cleared.** A
   * window that read 48% ten seconds ago has not become unknown because
   * one request did not land, and the server marks its own staleness when
   * it is the one that could not read (`stale`).
   */
  async _fetchAccountUsage({ force = false } = {}) {
    // An engine with no subscription windows to report has none to poll.
    // The router refuses the method rather than answering with an empty
    // list, and an empty list here would render as "you have no limits"
    // — a reading, where the truth is that this engine takes no such
    // measurement (AG-9).
    if (!supports(SURFACE.ACCOUNT_RATE_LIMITS)) return;
    try {
      const res = await withRpcTimeout(
        this.rpcExtract('ClaudeCodeService.get_account_usage', force),
        _FETCH_TIMEOUT_MS,
        'get_account_usage',
      );
      if (res && typeof res === 'object') this._accountUsage = res;
    } catch {
      // Deliberately quiet — see above.
    }
  }

  /**
   * MCP connection health, fetched alongside the breakdown and allowed
   * to fail silently.
   *
   * The breakdown is what this tab is for; health is a decoration on it.
   * So this swallows its own errors rather than letting a status call
   * that timed out — or a backend too old to have the method — replace a
   * page of usable numbers with an error paragraph. The cost of failure
   * is that server groups carry no status pill.
   *
   * @returns {Promise<object|null>} An `McpStatusResponse`, or null.
   */
  async _fetchMcpStatus() {
    // Null, which is already this method's "no answer" — the status
    // pills simply do not render. The distinction that matters is the
    // one `EngineHealth.mcp` got wrong before it was deleted: an engine
    // that serves no MCP inventory must not be reported as an engine
    // with zero servers.
    if (!supports(SURFACE.MCP_SERVER_INVENTORY)) return null;
    try {
      const res = await withRpcTimeout(
        this.rpcExtract('ClaudeCodeService.get_mcp_status'),
        _FETCH_TIMEOUT_MS,
        'get_mcp_status',
      );
      if (res && !res.error && Array.isArray(res.mcpServers)) return res;
    } catch {
      // Deliberately quiet — see above.
    }
    return null;
  }

  /**
   * Find out whether this client may drive the MCP controls.
   *
   * Same probe and the same default as the model selector's
   * `_probeModelAuthority`: no collab service registered means single-user,
   * which means we are the host.
   */
  async _probeMcpAuthority() {
    try {
      const role = await this.rpcExtract('Collab.get_collab_role');
      if (role && typeof role === 'object' && !role.error) {
        this._canControlMcp = role.is_localhost !== false;
        return;
      }
    } catch {
      // No collab service — single-user, and we are the host.
    }
    this._canControlMcp = true;
  }

  /**
   * Ask the engine to dial one server again.
   *
   * Offered only for `failed` and `needs-auth`, which is the loop the SDK
   * documents for this call. A `pending` row is mid-dial and re-dialling
   * it races the attempt already running; a `disabled` one was switched
   * off deliberately, and the answer there is Enable.
   *
   * The reply says `reconnecting`, not `connected`, and the toast repeats
   * it in those words. What happens next is the pill's to report — a green
   * pill written here would be this section claiming an outcome it has not
   * been told, on the one row that exists because a server was lying about
   * being fine.
   */
  async _reconnectServer(name) {
    await this._runMcpControl(
      name,
      'ClaudeCodeService.reconnect_mcp_server',
      [name],
      `Reconnecting ${name}…`,
    );
  }

  /**
   * Switch one server off, or back on.
   *
   * Enabling asks first; disabling does not. The RPC's own docstring makes
   * the argument — "the host is the one who decides which tools exist" —
   * and the two directions are not symmetric. Disabling only takes tools
   * away, and the reader reaching for it is usually looking at the token
   * cost in this very section and wants it gone. Enabling hands the agent
   * a set of capabilities it did not have a moment ago, so that is where
   * the friction belongs.
   *
   * The confirmation names the tool count when the engine gave one, and
   * says plainly that it did not when it has not. A disabled server
   * advertises nothing, so an absent count is the ordinary case here
   * rather than a fault — and a guessed number is one somebody would weigh
   * this decision against.
   */
  async _toggleServer(name, enabled, toolCount) {
    if (enabled) {
      const count = Number.isFinite(toolCount) && toolCount > 0
        ? `its ${toolCount} ${toolCount === 1 ? 'tool' : 'tools'}`
        : 'every tool it provides (the engine has not said how many — a '
          + 'server that is switched off advertises nothing)';
      const ok = window.confirm(
        `Enable "${name}"?\n\nThis hands the agent ${count}, and their `
        + 'description tokens join every request for the rest of this '
        + 'session.',
      );
      if (!ok) return;
    }
    await this._runMcpControl(
      name,
      'ClaudeCodeService.toggle_mcp_server',
      [name, enabled],
      `${name} ${enabled ? 'enabled' : 'disabled'}`,
    );
  }

  /**
   * Run one MCP control request and fold the answer back into the tab.
   *
   * Shared by both actions because the interesting part is the same three
   * things: hold the row so a second click cannot race the first, report a
   * refusal in the words the service used rather than a paraphrase, and
   * then re-read instead of patching state locally.
   *
   * The re-read is the whole breakdown, not just the status. Both calls
   * move the session's tool set, and that is two separate numbers on this
   * screen — the server's pill and its token cost — so refreshing one and
   * leaving the other is how a green pill ends up over a stale total. It
   * costs the breakdown's 3-14s, which is the price of the numbers being
   * about the tool set that exists now.
   */
  async _runMcpControl(name, method, args, done) {
    if (!this._canControlMcp || this._mcpBusy.has(name)) return;
    if (!this.rpcConnected) return;
    this._mcpBusy.add(name);
    this.requestUpdate();
    try {
      const res = await this.rpcExtract(method, ...args);
      if (res && typeof res === 'object' && res.error) {
        this._emitToast(
          res.error === 'restricted'
            ? res.reason || 'Only the host can change MCP servers'
            : res.error,
          'warning',
        );
        return;
      }
      this._emitToast(done, 'success');
    } catch (err) {
      this._emitToast(`${name}: ${err?.message || err}`, 'error');
      return;
    } finally {
      this._mcpBusy.delete(name);
      this.requestUpdate();
    }
    await this._refresh();
  }

  /** Same shape as the Settings tab's; the shell listens on `window`. */
  _emitToast(message, type = 'info') {
    window.dispatchEvent(
      new CustomEvent('aic-toast', { detail: { message, type }, bubbles: false }),
    );
  }

  /**
   * What the engine says about itself, for the Debug section.
   *
   * Fetched on the way *into* Debug rather than with the breakdown,
   * because `get_server_info` is another control request to the same
   * subprocess and the answer is the session's initialize reply — it
   * cannot change while the session lives. So: once per session, plus
   * whenever the reader presses Refresh while Debug is on screen.
   *
   * Health is fetched here too, for the client that connected between two
   * pushes and would otherwise show nothing until the next turn.
   */
  async _ensureDebug({ force = false } = {}) {
    if (this._debugLoading) return;
    if (!force && (this._serverInfo || this._debugError)) return;
    if (!this.rpcConnected) return;
    this._debugLoading = true;
    this._debugUpdate();
    // Read before the await, compared after. A push is fresher than this
    // fetch by definition — the server composed its answer before the push
    // it does not contain — so the fetch only writes when it is still the
    // newest thing we have.
    const seq = this._healthSeq;
    try {
      const [health, info, errors] = await Promise.all([
        this._fetchEngineHealth(),
        withRpcTimeout(
          this.rpcExtract('ClaudeCodeService.get_server_info'),
          _FETCH_TIMEOUT_MS,
          'get_server_info',
        ),
        this._fetchEngineErrors(),
      ]);
      // So: a fetch that answers nothing, *and* one that answers late,
      // both leave whatever arrived pushed in place. `mirror_gaps` moves
      // during a turn, and this fetch is seconds wide.
      if (health && this._healthSeq === seq) this._health = health;
      // No sequence guard, unlike health: nothing pushes this, so a late
      // answer cannot be older than what is already here.
      this._engineErrors = errors;
      if (info && info.error) {
        this._debugError = String(info.error);
      } else {
        this._serverInfo = info && typeof info === 'object' ? info : {};
        this._debugError = '';
      }
    } catch (err) {
      this._debugError = err?.message || 'Could not read server info.';
    } finally {
      this._debugLoading = false;
      this._debugUpdate();
    }
  }

  /**
   * The engine-health record, on request.
   *
   * Local state on the service rather than a control request, so this one
   * is cheap. Quiet on failure: `engine-health` pushes are the primary
   * source and the section says so when neither has landed.
   *
   * @returns {Promise<object|null>}
   */
  async _fetchEngineHealth() {
    try {
      const res = await withRpcTimeout(
        this.rpcExtract('ClaudeCodeService.get_engine_health'),
        _FETCH_TIMEOUT_MS,
        'get_engine_health',
      );
      if (res && typeof res === 'object' && !res.error) return res;
    } catch {
      // Deliberately quiet — see above.
    }
    return null;
  }

  /**
   * Why earlier engines would not start.
   *
   * Not quiet on failure, unlike `_fetchEngineHealth` above, and the
   * difference is that this read has no second source. Health arrives
   * pushed as well as fetched, so a failed fetch costs nothing; nothing
   * pushes this file, so a swallowed error would render as "no failures
   * recorded" — which is the one sentence here that must not be produced
   * by a read that did not happen.
   *
   * @returns {Promise<{errors?: object[], error?: string}>}
   */
  async _fetchEngineErrors() {
    try {
      const res = await withRpcTimeout(
        this.rpcExtract('ClaudeCodeService.get_engine_errors'),
        _FETCH_TIMEOUT_MS,
        'get_engine_errors',
      );
      if (res && typeof res === 'object') return res;
      return { error: 'The engine gave no answer for its error log.' };
    } catch (err) {
      return {
        error: `Could not read the engine error log: ${err?.message || err}`,
      };
    }
  }

  _goBackToChat() {
    this.dispatchEvent(
      new CustomEvent('request-dialog-tab', {
        detail: { tab: 'files' },
        bubbles: true,
        composed: true,
      }),
    );
  }

  /**
   * Open the SDK Surface tab.
   *
   * Placed in Debug's *Engine* section because that is where the SDK
   * version and the CLI pin are already reported, and "which features came
   * with that version" is the next question those two rows raise. The tab
   * is otherwise reachable only by Alt+5 — the dialog has no rendered tab
   * strip — so without a link from somewhere it is findable only by
   * someone who already knows it exists.
   */
  _goToSdkSurface() {
    this.dispatchEvent(
      new CustomEvent('request-dialog-tab', {
        detail: { tab: 'sdk-surface' },
        bubbles: true,
        composed: true,
      }),
    );
  }

  _minimizeDialog() {
    this.dispatchEvent(
      new CustomEvent('request-dialog-minimize', {
        bubbles: true,
        composed: true,
      }),
    );
  }

  _switchSection(id) {
    if (this._section === id) return;
    this._section = id;
    _saveSection(id);
    // Lazily, and only here: a reader who never opens Debug never spends a
    // control request on it.
    if (id === 'debug') this._ensureDebug();
  }

  /**
   * Show a section because something outside asked for it — a routed
   * slash command naming `tab:context#session`, today.
   *
   * Separate from `_switchSection` over one line: this does not write
   * the choice to localStorage. That key records the section its reader
   * was last on, and `/mcp` picking Session is not the reader picking
   * it — persisting would let a command quietly redecide where the tab
   * opens for every manual visit afterwards.
   *
   * An unknown id is ignored rather than blanking the body. The section
   * names come from the service's route table, so a mismatch means the
   * two have drifted, and the tab a user asked for opening on its usual
   * section is a far better answer than one rendering nothing.
   *
   * @param {string} id One of `_SECTIONS`.
   */
  showSection(id) {
    if (!_SECTIONS.some((s) => s.id === id)) return;
    if (this._section === id) return;
    this._section = id;
    if (id === 'debug') this._ensureDebug();
  }

  _toggleGroup(key) {
    if (this._openGroups.has(key)) this._openGroups.delete(key);
    else this._openGroups.add(key);
    this.requestUpdate();
  }

  /**
   * Open a memory file in the viewer.
   *
   * Minimizes this dialog as well as navigating, because the viewer is
   * behind it: a click that opens a file under an opaque panel is
   * indistinguishable from a click that did nothing.
   *
   * The path handed over is the *absolute* one the engine reported,
   * like every other `navigate-file` dispatcher's. The shell's handler
   * relativises at the choke point (`app-shell/viewers.js`), which is
   * where that conversion is already owned; converting here as well
   * would be the per-dispatcher duplication that comment argues
   * against.
   */
  _openMemoryFile(path) {
    if (!path) return;
    window.dispatchEvent(
      new CustomEvent('navigate-file', { detail: { path }, bubbles: false }),
    );
    this._minimizeDialog();
  }

  // ---------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------

  render() {
    return html`
      <div class="toolbar">
        <button
          class="back-btn"
          title="Back to chat"
          @click=${this._goBackToChat}
        >← Chat</button>
        <button
          class="tool-btn"
          ?disabled=${this._loading || !this.rpcConnected}
          title="Ask the engine for a fresh breakdown"
          @click=${() => this._refresh({ force: true })}
        >${this._loading ? 'Reading…' : '↻ Refresh'}</button>
        ${this._stale
          ? html`<span class="stale-badge">● stale</span>`
          : ''}
        <span class="spacer"></span>
        <button
          class="tool-btn"
          title="Minimize"
          @click=${this._minimizeDialog}
        >▾</button>
      </div>
      ${this._usage || this._error ? this._renderSegmented() : ''}
      <div class="content">${this._renderBody()}</div>
    `;
  }

  /**
   * The section selector, in its own row rather than in the toolbar:
   * the toolbar's buttons act on the *breakdown* — go back, refetch,
   * minimize — and these choose which part of one breakdown to read.
   *
   * Shown once there is a breakdown *or* an error. The error case is not a
   * courtesy: a breakdown that failed is the moment Debug is worth the most,
   * and gating the control on success alone hid it exactly then.
   */
  _renderSegmented() {
    return html`
      <div class="segmented" role="tablist" aria-label="Context sections">
        ${_SECTIONS.map((s) => html`
          <button
            class="seg"
            role="tab"
            aria-selected=${this._section === s.id ? 'true' : 'false'}
            @click=${() => this._switchSection(s.id)}
          >${s.label}</button>
        `)}
      </div>
    `;
  }

  _renderBody() {
    // Ahead of the error branch on purpose: Debug reads the engine, not the
    // breakdown, and it is the section a reader reaches for when the
    // breakdown is the thing that failed.
    if (this._section === 'debug') {
      return html`${this._renderDebugSection()}${this._renderFooter()}`;
    }
    // The rate-limit section rides above both of the no-breakdown
    // branches, because it is the one thing on this tab that **does not
    // come from the engine**. A reader who has been cut off by a weekly
    // limit has no session to interrogate — which is exactly when the
    // breakdown fails and exactly when they came to look at their
    // windows. Leaving it inside the success branch meant the panel
    // vanished precisely when it was the reason for opening the tab.
    if (this._error && !this._usage) {
      // Red only for a *failure*. 'no-engine' is the ordinary state of a
      // window nobody has sent a prompt in yet, and painting it in the
      // error colour tells a reader something is wrong at the one moment
      // nothing is — the same "unbuilt reads as broken" fault the engine
      // notice was added for. The Settings tab already answers this exact
      // state in secondary grey ("the list arrives with the first turn"),
      // so this is the tab that was out of step rather than a new idiom.
      const calm = this._errorReason === 'no-engine';
      return html`
        ${this._renderRateLimits()}
        <p class=${calm ? 'empty' : 'error'}>${this._error}</p>
        ${this._renderErrorNote()}
      `;
    }
    if (!this._usage) {
      return html`
        ${this._renderRateLimits()}
        <p class="empty">
          ${this._loading ? 'Reading context…' : 'No breakdown yet.'}
        </p>
      `;
    }
    return html`
      ${this._section === 'session'
        ? this._renderSessionSection()
        : this._renderUsageSection()}
      ${this._renderFooter()}
    `;
  }

  /**
   * What to do about the error above it.
   *
   * There is no session and a request failed are opposite situations
   * with opposite answers — wait, or try again — and this note used to
   * give the waiting answer to both. A reader with a perfectly healthy
   * session was told to connect one, which reads as the tab being
   * broken rather than the call having been slow.
   *
   * The unlabelled case gets the advice that is true either way. A
   * backend too old to send a reason lands there, and so would a
   * reason this build has never heard of.
   */
  _renderErrorNote() {
    if (this._errorReason === 'no-engine') {
      return html`<p class="note">
        The breakdown comes from the running engine, so it is
        unavailable until a session is connected.
      </p>`;
    }
    if (this._errorReason === 'failed') {
      return html`<p class="note">
        A request that failed, not a session that is missing — Refresh
        asks again. The engine answers this one beside a live turn, so a
        turn in flight makes it slow rather than unavailable.
      </p>`;
    }
    return html`<p class="note">
      The breakdown comes from the running engine. Refresh asks again;
      with no session yet, it stays unavailable until one starts.
    </p>`;
  }

  /**
   * Usage — how full the window is, what is filling it, and how long
   * that leaves. Everything here is about the window's *capacity*;
   * what the session is made of is the Session section's question.
   */
  _renderUsageSection() {
    // Derived once and handed down: three of these sections read the
    // same `messageBreakdown`, and re-deriving it per section would put
    // three sorts and three filters in every render for one payload
    // that cannot change between them.
    const comp = messageComposition(this._usage);
    return html`
      ${this._renderSessionUsage()}
      ${this._renderRateLimits()}
      ${this._renderHeadline()}
      ${this._renderCategories()}
      ${this._renderMessageBreakdown(comp)}
      ${this._renderToolTraffic(comp)}
      ${this._renderAttachments(comp)}
    `;
  }

  /**
   * Session — what this conversation has cost so far.
   *
   * This block and the gauges under it are what `/usage` opens onto, and
   * until 2026-08-29 neither existed: the command's own reply promised "the
   * Context tab's cost and per-model token breakdown" and the tab rendered
   * context-window composition and nothing else. The figures were on the
   * wire the whole time — they were rendered only in the per-turn HUD, which
   * auto-hides after 8.8 seconds and never appears at all for a session that
   * has not run a turn.
   *
   * **Cumulative on purpose, which is the opposite of every other cost on
   * screen.** `total_cost_usd` and `model_usage` are the session's running
   * totals; rendering them as a turn's is the bug `turn-cost.js` exists to
   * prevent, and this is the one question they answer directly. The heading
   * says "Session" and every row is labelled, so the two readings cannot be
   * confused by anyone reading the panel rather than the source.
   *
   * Absent rather than empty before the first priced result: a session that
   * has run nothing has no cost, and `$0.0000` is a claim about it.
   *
   * The table goes with the rows, and the "no per-model figures yet" note
   * stands on `rows.length` alone. Both used to be wrong in the same
   * state: the note was gated on there being a cost, but the section
   * renders on *any* of cost, seconds or rows — so a session with a
   * connected engine and no priced result yet drew three column headings
   * over nothing, with no word about why. Headings promising data that
   * never arrives read as a load that failed.
   */
  _renderSessionUsage() {
    const usage = this._sessionUsage;
    // The *cost* hides, not the section. AG-6 is that this engine reports
    // usage in tokens and no USD is invented for it — so the per-model
    // rows below are exactly as meaningful either way, and dropping them
    // with the dollar figure would hide a measurement that was taken to
    // hide one that was not. `null` is already this method's "no figure",
    // so the "so far" line and the priced-result note fall away with it.
    const cost = supports(SURFACE.USD_COST)
      ? formatCost(usage?.total_cost_usd)
      : null;
    const seconds = _sessionSeconds(usage);
    const models = usage?.model_usage && typeof usage.model_usage === 'object'
      ? Object.entries(usage.model_usage)
      : [];
    const rows = models
      .map(([key, entry]) => ({
        model: entry?.canonicalModel || key,
        ...usageSplit(entry),
      }))
      .filter((row) => row.total > 0)
      .sort((a, b) => b.total - a.total);
    if (cost === null && seconds === null && rows.length === 0) return '';

    return html`
      <section>
        <h3>
          Session
          ${cost !== null
            ? html`<span class="sub">— ${cost} so far</span>`
            : ''}
        </h3>
        ${rows.length > 0
          ? html`
            <table>
              <thead>
                <tr>
                  <th>Model</th>
                  <th class="num">Prompt</th>
                  <th class="num">Completion</th>
                </tr>
              </thead>
              <tbody>
                ${rows.map((row) => html`
                  <tr>
                    <td title=${row.model}>${row.model}</td>
                    <td
                      class="num"
                      title=${`${row.input.toLocaleString()} uncached · ${row.cacheRead.toLocaleString()} cache read · ${row.cacheCreation.toLocaleString()} cache write`}
                    >${_fmtTokens(row.prompt)}</td>
                    <td class="num">${_fmtTokens(row.output)}</td>
                  </tr>
                `)}
              </tbody>
            </table>
          `
          : ''}
        ${seconds !== null
          ? html`<p class="note">
              ${_fmtDuration(seconds)} since the engine connected. No API-time
              figure sits beside it: the CLI does not say whether its duration
              fields are per-turn or cumulative, and reading that wrong is
              silent.
            </p>`
          : ''}
        ${rows.length === 0
          ? html`<p class="note">
              No per-model figures yet — the engine reports them with the first
              priced result.
            </p>`
          : ''}
      </section>
    `;
  }

  /**
   * Rate limits — one gauge per window the account has open.
   *
   * **Two sources, and only one of them has the figures.** The engine's
   * `RateLimitEvent` channel emits on a *transition* and carried no
   * utilisation at all in the record measured 2026-08-29, which is what
   * put a grey "no figure" where the CLI's own `/usage` was showing 37%
   * and two more windows besides. `ClaudeCodeService.get_account_usage`
   * reads the endpoint the CLI itself reads, so `_accountUsage.windows`
   * is the panel and `_rateLimits` is reduced to the alarms it always
   * was. When the account read is unavailable the event records are drawn
   * as gauges again, exactly as before, because a transition notice is
   * still better than an empty section.
   *
   * The account records arrive **already in this file's units** — fraction
   * and Unix seconds — converted once on the server, where the mismatch
   * with the endpoint's own percent-and-ISO is documented
   * (`account_usage.py`). Nothing here needs to know which source a record
   * came from to read its numbers, which is the point.
   *
   * **Expiry is decided here and only here.** The server serves each record
   * raw, so a window whose `resets_at` has passed is dropped by
   * `windowIsOpen` — the same function the HUD and the chat toast call, so
   * there is one definition of "still open" and three readers rather than
   * three definitions (`specs5/next.md` § C3). A window with **no** reset
   * counts as open, which is what shows the per-model weekly cap for a
   * model you have not used this week: 0%, no reset, and a real answer.
   *
   * Most-constrained first, unlike the HUD's single record, which shows the
   * window that most recently *transitioned*. A list has room to rank and a
   * gauge does not; the window nearest its ceiling is the one the reader
   * came to find.
   */
  _renderRateLimits() {
    // Two surfaces, and the section needs neither to be present to be
    // worth hiding: `account_rate_limits` feeds the gauges and
    // `rate_limit_events` feeds the notices, so an engine that reports
    // neither has nothing to put here. Hidden rather than shown empty —
    // a "Rate limits" heading over nothing reads as "you have none",
    // which is a claim rather than an absence (AG-9).
    if (
      !supports(SURFACE.ACCOUNT_RATE_LIMITS)
      && !supports(SURFACE.RATE_LIMIT_EVENTS)
    ) {
      return '';
    }
    const account = this._accountUsage;
    const windows =
      account && account.ok && Array.isArray(account.windows)
        ? account.windows
            .filter((w) => windowIsOpen(w))
            .sort((a, b) => (utilizationPercent(b) ?? 0) - (utilizationPercent(a) ?? 0))
        : [];
    const open = (this._rateLimits || [])
      .filter((record) => windowIsOpen(record))
      .sort((a, b) => (utilizationPercent(b) ?? 0) - (utilizationPercent(a) ?? 0));
    // The account read is the answer when it came. The event records are
    // then demoted to what they are — notices about transitions — and
    // only the ones carrying something the gauges cannot say are kept:
    // an overage rejection, or a window actively refusing calls. Neither
    // is in the account payload's vocabulary.
    //
    // **Demoted means the headline goes too, not just the row.** Keeping
    // the gauge markup here drew "5-hour — no figure" directly beneath
    // the 5-hour gauge reading 48%: one window claiming to be two, and
    // the grey "no figure" contradicting the real number right above it.
    // So a kept record renders as its sentence alone (`_renderEventNotice`)
    // and only becomes a gauge again when there are no account windows for
    // it to contradict.
    const notices = windows.length
      ? open.filter((r) => r.status === 'rejected' || r.overage_status)
      : open;
    if (!windows.length && !notices.length && !(account && account.ok === false)) {
      return '';
    }

    return html`
      <section>
        <h3>
          Rate limits
          <span class="sub"
            >— ${windows.length
              ? `${windows.length} window${windows.length === 1 ? '' : 's'}`
              : `${open.length} open window${open.length === 1 ? '' : 's'}`}</span
          >
        </h3>
        ${windows.map((w) => this._renderAccountWindow(w))}
        ${this._renderAccountNote(notices.length > 0)}
        ${windows.length
          ? notices.map((record) => this._renderEventNotice(record))
          : notices.map((record) => this._renderEventLimit(record))}
      </section>
    `;
  }

  /**
   * One event record as a gauge — the pre-account rendering, unchanged.
   *
   * Reached only when the account read gave nothing, which is when a
   * transition notice is the best information on the page.
   */
  _renderEventLimit(record) {
    const pct = utilizationPercent(record);
    const rejected = record.status === 'rejected';
    // **No figure is not a figure of zero**, and this is the one thing
    // this section must not get wrong. `utilization` is absent from real
    // records — the CLI's whole payload is six fields and that is not one
    // of them (§ The Rate-Limit Channel Is An Alarm, Not A Usage Panel) —
    // so falling through to `_pctColor(0)` painted "we were not told" in
    // the healthy band, above an empty bar that reads as 0% used. An empty
    // list does not say "no servers", it says "no answer": the same rule,
    // one surface over.
    const unknown = pct === null && !rejected;
    // Red at any figure when the limit is refusing calls: an overage
    // cut-off can reject at a utilisation the bands would call healthy.
    const color = rejected
      ? '#f85149'
      : unknown
        ? 'var(--text-muted, #8b949e)'
        : _pctColor(pct);
    const resets = formatResetTime(record.resets_at);
    return html`
      <div class="limit">
        <div class="headline">
          <span class="of">${limitTypeLabel(record.rate_limit_type)}</span>
          <span class="pct" style="color: ${color}">
            ${rejected ? 'reached' : unknown ? 'no figure' : `${Math.round(pct)}%`}
          </span>
        </div>
        ${unknown
          ? ''
          : html`<div class="bar" role="presentation">
              <div
                class="bar-seg"
                style=${`width:${Math.min(100, pct)}%;background:${color}`}
              ></div>
            </div>`}
        ${resets ? html`<p class="note">Resets ${resets}</p>` : ''}
        ${unknown
          ? html`<p class="note">
              Reported without a utilisation, which is the usual case: the
              percentage the CLI's own <code>/usage</code> shows is not in the
              event it sends us. The window and its reset are what is known.
            </p>`
          : ''}
        ${this._renderOverageNote(record)}
      </div>
    `;
  }

  /**
   * A kept event record beside the gauges: its sentence, and no headline.
   *
   * What survives the demotion is only what the account payload has no
   * words for — a window actively refusing calls, and the state of
   * overage. The window's *name* is carried in the sentence rather than
   * in a heading, and there is deliberately **no `.limit` wrapper**: that
   * class is the gauge, and a notice wearing it is a fourth window to
   * anything counting them, including this panel's own tests.
   */
  _renderEventNotice(record) {
    const label = limitTypeLabel(record.rate_limit_type);
    return html`
      ${record.status === 'rejected'
        ? html`<p class="note" style="color: #f85149">
            The ${label || 'rate'} limit is refusing calls right now.
          </p>`
        : ''}
      ${this._renderOverageNote(record)}
    `;
  }

  /**
   * Overage, as the engine reported it.
   *
   * Shared by both renderings above because it is the one thing on this
   * channel worth keeping either way, and because it says something the
   * account endpoint does not: `overage_status` is *why pay-as-you-go is
   * unavailable*, and `is_using_overage` separates "you have overage and
   * are not on it" from "you are on it right now".
   */
  _renderOverageNote(record) {
    if (record.overage_status === 'rejected') {
      return html`<p class="note">
        Overage unavailable${
          record.overage_disabled_reason
            ? html` — ${record.overage_disabled_reason.replace(/_/g, ' ')}`
            : ''
        }, so this window is a hard ceiling rather than a warning.
      </p>`;
    }
    if (record.overage_status) {
      return html`<p class="note">
        Overage ${record.overage_status.replace(/_/g, ' ')}${
          record.is_using_overage ? ', and in use' : ''
        }.
      </p>`;
    }
    return '';
  }

  /**
   * One gauge from the account read.
   *
   * Simpler than its event-record neighbour by construction: this source
   * always carries a figure, so there is no "no figure" branch and no
   * grey band to fall into. The label is the server's — a scoped weekly
   * window is named after the model the account holds the cap for, and
   * that name is read from the payload rather than mapped from an enum,
   * because a per-model cap for a model shipped tomorrow is exactly the
   * row the fixed enum could never carry.
   *
   * A missing `resets_at` draws no reset line rather than a placeholder.
   * The window is real either way, and the endpoint leaves it null for a
   * cap that has not started counting.
   */
  _renderAccountWindow(w) {
    const pct = utilizationPercent(w);
    if (pct === null) return '';
    const color = _pctColor(pct);
    const resets = formatResetTime(w.resets_at);
    return html`
      <div class="limit">
        <div class="headline">
          <span class="of">${w.label || 'Window'}</span>
          <span class="pct" style="color: ${color}">${Math.round(pct)}%</span>
        </div>
        <div class="bar" role="presentation">
          <div class="bar-seg" style=${`width:${Math.min(100, pct)}%;background:${color}`}></div>
        </div>
        ${resets ? html`<p class="note">Resets ${resets}</p>` : ''}
      </div>
    `;
  }

  /**
   * Where the gauges came from, or why there are none.
   *
   * **The failure sentence is the feature.** Every reason the server can
   * name is a different thing for the reader to do — refresh a login, run
   * a turn, or stop expecting subscription figures on a machine whose
   * turns bill to an API key — and collapsing them into "unavailable"
   * sends all three of those readers to the wrong place. So the server's
   * `detail` is printed as written.
   */
  _renderAccountNote(hasNotices) {
    const account = this._accountUsage;
    if (!account || typeof account !== 'object') return '';
    if (account.ok) {
      if (!account.stale) return '';
      // A figure from the last good read, over a read that failed. Said
      // out loud, because an unqualified percentage is a claim about now.
      return html`<p class="note">
        Last successful reading — the account endpoint could not be reached
        just now${account.stale_detail ? html` (${account.stale_detail})` : ''}.
      </p>`;
    }
    return html`<p class="note">
      Account usage unavailable. ${account.detail ? html`${account.detail} ` : ''}${
        hasNotices
          ? 'What follows is the engine reporting a change, not a reading.'
          : ''
      }
    </p>`;
  }

  /**
   * Session — what this session is made of and what each part costs.
   *
   * Everything here is cost the session was *started* with, in the order
   * the spec lists it: the files the user wrote, the prompt the engine
   * prepended, the tools it can reach, and the inventory it can draw on.
   * None of it moves when a turn runs, which is what separates it from
   * Usage.
   */
  _renderSessionSection() {
    const health = mcpHealth(this._mcpStatus);
    const sections = [
      this._renderMemoryFiles(),
      this._renderSystemPrompt(),
      this._renderTools(health),
      this._renderAgents(),
      this._renderSkills(),
      this._renderSlashCommands(),
    ].filter((s) => s !== '');
    if (sections.length === 0) {
      return html`<p class="empty">
        The engine reported no memory files, prompt sections, tools or
        inventory for this session.
      </p>`;
    }
    return html`${sections}`;
  }

  /**
   * The headline reports two percentages against two denominators,
   * both labelled, because there is no single honest one.
   *
   * The big number is the engine's own, against the raw window, so
   * this view and `/context` cannot disagree. On its own it is
   * reassuring to the point of useless — it reads 78% when a compact
   * is one turn away — so the line beneath it measures the same tokens
   * against `autoCompactThreshold`, which is where the session
   * actually gives out.
   *
   * The big number is deliberately *coloured* by that second figure
   * rather than by itself: the warning has to land on the thing being
   * looked at. A green 70% above an amber "83.8% of the way to an
   * autocompact" is a mixed signal, and the green wins. So the digits
   * stay in parity with `/context` while the colour tracks the event
   * the user cares about, and the note spells out which is which.
   *
   * The mark on the bar is the third telling of the same fact, and the
   * only one that is spatial. Both numbers are answers to "how full";
   * the mark answers "how much further", which is the question a bar is
   * for.
   */
  _renderHeadline() {
    const u = this._usage;
    const total = Number(u.totalTokens) || 0;
    const max = Number(u.maxTokens) || 0;
    const clamped = windowPercent(u);
    const warnPct = warningPercent(u);
    const limit = compactionLimit(u);
    const toLimit = compactionPercent(u);
    const autoCompacts = u.isAutoCompactEnabled !== false;
    const markPct = thresholdPercent(u);
    const over = overLimit(u);
    // Segment the fill by what is actually in the window. The engine's
    // `categories` also contains "Free space" and "Autocompact
    // buffer", which together make up the rest of the window — segment
    // by all of them and the bar is permanently 100% full.
    const { content, verified } = partitionCategories(u);
    const segmented = verified && content.length > 0 && max > 0;

    return html`
      <section>
        <div class="headline">
          <span class="pct" style="color: ${_pctColor(warnPct)}">
            ${clamped.toFixed(1)}%
          </span>
          <span class="of">
            ${total.toLocaleString()} / ${max.toLocaleString()} tokens
          </span>
        </div>
        <div class="bar-wrap">
          <div class="bar gauge">
            ${segmented
              ? content.map((c) => html`
                  <div
                    class="bar-seg"
                    style="width: ${(Number(c.tokens) / max) * 100}%;
                           background: ${categoryColor(c.color)};"
                    title="${c.name}: ${_fmtTokens(c.tokens)}"
                  ></div>
                `)
              : html`<div
                  class="bar-seg"
                  style="width: ${clamped}%; background: ${_pctColor(clamped)};"
                ></div>`}
          </div>
          ${markPct != null ? html`
            <div
              class="mark"
              style="left: ${markPct}%"
              title="Autocompact triggers here, at ${limit.toLocaleString()} tokens"
            ></div>
          ` : ''}
        </div>
        ${markPct != null ? html`
          <div class="mark-note">
            <span>Autocompact at ${markPct.toFixed(1)}% of the window</span>
            <span>${max.toLocaleString()}</span>
          </div>
        ` : ''}
        ${over ? html`
          <p class="error">
            ${over.kind === 'hard_limit'
              ? html`Past the model's ${over.window.toLocaleString()}-token
                  limit by ${over.over.toLocaleString()} tokens.`
              : html`${over.over.toLocaleString()} tokens past the
                  ${over.window.toLocaleString()}-token compaction
                  window.`}
            ${autoCompacts
              ? 'Autocompact should bring it back down on the next turn.'
              : 'Autocompact is off, so nothing will reduce it.'}
          </p>
        ` : ''}
        ${toLimit != null && autoCompacts && limit < max ? html`
          <p class="note" style="color: ${_pctColor(toLimit)}">
            ${toLimit.toFixed(1)}% of the way to an autocompact, which
            triggers at ${limit.toLocaleString()} tokens —
            ${Math.max(0, limit - total).toLocaleString()} tokens of
            room left. The remaining
            ${(max - limit).toLocaleString()} are reserved for the
            summary.
          </p>
        ` : ''}
        ${!autoCompacts ? html`
          <p class="warn">
            Autocompact is off for this session, so the bar carries no
            mark. Reaching the limit fails the turn rather than
            summarising the history.
          </p>
        ` : ''}
        ${u.model
          ? html`<p class="model-note">Measured for ${u.model}.</p>`
          : ''}
      </section>
    `;
  }

  /**
   * The category legend: every row the bar above is drawn from, plus
   * the rows it deliberately leaves out — the room left over and the
   * deferred budget — since those are the ones that explain why the bar
   * is shorter than the window.
   *
   * Zero-token rows are dropped, as the spec asks. A row whose count is
   * *unusable* is kept and shows an em dash: a category the engine named
   * but could not count is worth seeing, and a category that costs
   * nothing is not.
   */
  _renderCategories() {
    const cats = (Array.isArray(this._usage.categories)
      ? this._usage.categories
      : []
    )
      .filter((c) => {
        const n = Number(c?.tokens);
        return !Number.isFinite(n) || n > 0;
      })
      .sort((a, b) => (Number(b.tokens) || 0) - (Number(a.tokens) || 0));
    if (cats.length === 0) {
      return html`<section>
        <h3>Categories</h3>
        <p class="empty">The engine reported no categories.</p>
      </section>`;
    }
    // Share is against the window, not against `totalTokens`. The
    // engine's rows include the room left over, so dividing by the
    // tokens in use rendered "Free space — 692.0%".
    const max = Number(this._usage.maxTokens) || 0;
    return html`
      <section>
        <h3>Categories</h3>
        <table>
          <thead>
            <tr>
              <th>Category</th>
              <th class="num">Tokens</th>
              <th class="num">Share of window</th>
            </tr>
          </thead>
          <tbody>
            ${cats.map((c) => html`
              <tr class=${c.isDeferred ? 'deferred' : ''}>
                <td>
                  <span
                    class="swatch"
                    style="background: ${categoryColor(c.color)}"
                  ></span>${c.name}${c.isDeferred
                    && !/\(deferred\)/i.test(String(c.name ?? ''))
                    // The engine names some rows "System tools
                    // (deferred)" and also flags them, which rendered
                    // as "System tools (deferred) (deferred)".
                    ? html` <span
                        class="note"
                        title="Budgeted by the engine but not loaded into the window yet"
                      >(deferred)</span>`
                    : ''}
                </td>
                <td class="num">${_fmtTokens(c.tokens)}</td>
                <td class="num">
                  ${max > 0
                    ? `${((Number(c.tokens) / max) * 100).toFixed(1)}%`
                    : '—'}
                </td>
              </tr>
            `)}
          </tbody>
        </table>
      </section>
    `;
  }

  /**
   * What the conversation itself is made of.
   *
   * The one part of the window the user changes by *talking*, so it gets
   * the same treatment as the window: a proportional bar and a legend
   * under it. Everything else in this tab is fixed cost the session was
   * started with.
   *
   * Drawn against the parts' own sum rather than against the Messages
   * category, so the segments always tile the bar. The two agree to
   * within a token in the normal case; when they do not, the note says
   * which is which rather than leaving a bar that quietly does not add
   * up — see `messageComposition`.
   */
  _renderMessageBreakdown(comp) {
    if (!comp || comp.parts.length === 0) return '';
    const { parts, partsTokens, messagesTokens, reconciled } = comp;
    return html`
      <section>
        <h3>
          Messages
          <span class="sub">— ${_fmtTokens(partsTokens)} tokens</span>
        </h3>
        <div class="bar">
          ${parts.map((p) => html`
            <div
              class="bar-seg"
              style="width: ${(p.tokens / partsTokens) * 100}%;
                     background: ${p.color};"
              title="${p.label}: ${_fmtTokens(p.tokens)}"
            ></div>
          `)}
        </div>
        <table>
          <thead>
            <tr>
              <th>Part</th>
              <th class="num">Tokens</th>
              <th class="num">Share of messages</th>
            </tr>
          </thead>
          <tbody>
            ${parts.map((p) => html`
              <tr>
                <td>
                  <span
                    class="swatch"
                    style="background: ${p.color}"
                  ></span>${p.label}
                </td>
                <td class="num">${_fmtTokens(p.tokens)}</td>
                <td class="num">
                  ${((p.tokens / partsTokens) * 100).toFixed(1)}%
                </td>
              </tr>
            `)}
          </tbody>
        </table>
        ${!reconciled && messagesTokens ? html`
          <p class="note">
            These parts sum to ${partsTokens.toLocaleString()} tokens
            against the ${messagesTokens.toLocaleString()} in the
            Messages category above. The per-part figures are the
            engine's own estimate over the same history, so read the
            shares as proportions rather than as a second count.
          </p>
        ` : ''}
      </section>
    `;
  }

  /**
   * Which tools the conversation is paying for, and for what.
   *
   * This is the section that answers the spec's "names the `aic-dc` tools
   * it is paying for": the rows carry the engine's own tool names, so
   * the bridge's tools appear here under the names they were registered
   * with, alongside every built-in they compete with for the window.
   *
   * Calls and results are separate columns because they are separately
   * fixable. A heavy call column is a request passing too much in; a
   * heavy result column is a tool answering with more than was asked
   * for, and only one of those is the caller's to change.
   *
   * Both headers name the unit. They used to read "Calls" and "Results",
   * which a live reader took for counts — every cell here is tokens, and
   * "Calls: 4.2k" against a tool called four times is a wrong number
   * rather than an unclear one.
   */
  _renderToolTraffic(comp) {
    if (!comp || comp.byTool.length === 0) return '';
    const { byTool } = comp;
    const total = byTool.reduce((sum, t) => sum + t.tokens, 0);
    return html`
      <section>
        <h3>
          Tool traffic
          <span class="sub">
            — ${byTool.length} ${byTool.length === 1 ? 'tool' : 'tools'},
            ${_fmtTokens(total)} tokens
          </span>
        </h3>
        <table>
          <thead>
            <tr>
              <th>Tool</th>
              <th class="num">Call tokens</th>
              <th class="num">Result tokens</th>
              <th class="num">Total</th>
            </tr>
          </thead>
          <tbody>
            ${byTool.map((t) => html`
              <tr>
                <td>${t.name}</td>
                <td class="num">${_fmtTokens(t.callTokens)}</td>
                <td class="num">${_fmtTokens(t.resultTokens)}</td>
                <td class="num">${_fmtTokens(t.tokens)}</td>
              </tr>
            `)}
          </tbody>
        </table>
      </section>
    `;
  }

  /**
   * Attachments by kind — pasted text, images, file references.
   *
   * Its own section rather than a row in the message table because the
   * message table's Attachments row is the total, and the thing worth
   * knowing is which kind of attachment it was.
   */
  _renderAttachments(comp) {
    if (!comp || comp.byAttachment.length === 0) return '';
    const { byAttachment } = comp;
    const total = byAttachment.reduce((sum, a) => sum + a.tokens, 0);
    return html`
      <section>
        <h3>
          Attachments
          <span class="sub">— ${_fmtTokens(total)} tokens</span>
        </h3>
        <table>
          <thead>
            <tr>
              <th>Type</th>
              <th class="num">Tokens</th>
            </tr>
          </thead>
          <tbody>
            ${byAttachment.map((a) => html`
              <tr>
                <td>${a.name}</td>
                <td class="num">${_fmtTokens(a.tokens)}</td>
              </tr>
            `)}
          </tbody>
        </table>
      </section>
    `;
  }

  /**
   * CLAUDE.md and other memory files the CLI loaded.
   *
   * First in the section and the only clickable table in the tab, because
   * these are the one part of the fixed cost the user can *edit*. Knowing
   * `CLAUDE.md` costs 4.3K tokens is only half an answer; the other half
   * is being in the file.
   *
   * A file inside the repo is named the way every other view in this app
   * names files — relative to the root — with the engine's absolute path
   * on the row's tooltip. One outside it keeps the absolute path, because
   * that is the only name it has here.
   */
  _renderMemoryFiles() {
    const files = Array.isArray(this._usage.memoryFiles)
      ? this._usage.memoryFiles
      : [];
    if (files.length === 0) return '';
    const total = files.reduce((sum, f) => sum + (Number(f.tokens) || 0), 0);
    return html`
      <section>
        <h3>
          Memory files
          <span class="sub">
            — ${files.length} ${files.length === 1 ? 'file' : 'files'},
            ${_fmtTokens(total)} tokens
          </span>
        </h3>
        <table>
          <thead>
            <tr>
              <th>Path</th>
              <th>Type</th>
              <th class="num">Tokens</th>
            </tr>
          </thead>
          <tbody>
            ${files.map((f) => {
              const path = f.path || f.name || '';
              // One question, asked once: `toRepoPath` gives back a
              // *different* string only when the path is absolute and
              // inside the repo root, which is exactly the condition
              // for a row being openable — every repo read rejects an
              // absolute path, and `~/.claude/CLAUDE.md` is outside the
              // repo entirely. So the rule that names the file is also
              // the rule that decides whether it is a link, and rows it
              // does not recognise stay text rather than offering a
              // click that would fail.
              //
              // The service used to answer this with a `relPath` field
              // (`next.md` § C3). It no longer does, and this is the
              // only reader that would have wanted it.
              const rel = toRepoPath(path);
              const openable = rel !== path;
              return html`
                <tr>
                  <td class="path" title=${path}>
                    ${openable
                      ? html`<button
                          class="link"
                          title="Open ${rel} in the viewer"
                          @click=${() => this._openMemoryFile(path)}
                        >${rel}</button>`
                      : path || '—'}
                  </td>
                  <td>${f.type || '—'}</td>
                  <td class="num">${_fmtTokens(f.tokens)}</td>
                </tr>
              `;
            })}
          </tbody>
        </table>
      </section>
    `;
  }

  /**
   * What the engine prepended before the conversation started.
   *
   * Unlike memory files this is not editable, and that is the point of
   * showing it: it is the floor under every session, and a reader who
   * knows the floor is 12K tokens stops trying to explain it away as
   * something they did.
   */
  _renderSystemPrompt() {
    const rows = (Array.isArray(this._usage.systemPromptSections)
      ? this._usage.systemPromptSections
      : []
    )
      .filter((s) => s && typeof s === 'object')
      .sort((a, b) => (Number(b.tokens) || 0) - (Number(a.tokens) || 0));
    if (rows.length === 0) return '';
    const total = rows.reduce((sum, s) => sum + (Number(s.tokens) || 0), 0);
    return html`
      <section>
        <h3>
          System prompt
          <span class="sub">
            — ${rows.length} ${rows.length === 1 ? 'section' : 'sections'},
            ${_fmtTokens(total)} tokens
          </span>
        </h3>
        <table>
          <thead>
            <tr>
              <th>Section</th>
              <th class="num">Tokens</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map((s) => html`
              <tr>
                <td>${s.name || '—'}</td>
                <td class="num">${_fmtTokens(s.tokens)}</td>
              </tr>
            `)}
          </tbody>
        </table>
      </section>
    `;
  }

  /**
   * Every tool the session can reach, grouped by where it comes from.
   *
   * One section with collapsible groups rather than three flat tables.
   * The flat `mcpTools` table this replaces listed 35 `aic-dc` rows next
   * to two from another server, which made "what does each server cost
   * me" — the question a per-server view exists to answer — a
   * subtraction the reader had to do, and buried the rest of the section
   * under it.
   *
   * Our own `aic-dc` server appears here like any other. That is a
   * deliberate spec invariant, not an accident of iteration: the bridge
   * competes for the same window as everything else and this is the one
   * place that keeps us honest about its price.
   */
  _renderTools(health) {
    const u = this._usage;
    const builtin = (Array.isArray(u.systemTools) ? u.systemTools : []).filter(
      (t) => t && typeof t === 'object',
    );
    const deferredBuiltin = (Array.isArray(u.deferredBuiltinTools)
      ? u.deferredBuiltinTools
      : []
    ).filter((t) => t && typeof t === 'object');
    const groups = [];
    if (builtin.length) {
      groups.push({
        key: 'builtin',
        name: 'Built-in tools',
        tools: builtin
          .map((t) => ({
            name: String(t.name ?? '—'),
            tokens: Number(t.tokens) || 0,
            deferred: t.isLoaded === false,
          }))
          .sort((a, b) => b.tokens - a.tokens),
      });
    }
    if (deferredBuiltin.length) {
      groups.push({
        key: 'builtin-deferred',
        name: 'Built-in tools, deferred',
        tools: deferredBuiltin
          .map((t) => ({
            name: String(t.name ?? '—'),
            tokens: Number(t.tokens) || 0,
            // The engine puts these in their own list *because* they are
            // deferred; `isLoaded` is present but redundant here.
            deferred: true,
          }))
          .sort((a, b) => b.tokens - a.tokens),
      });
    }
    for (const g of serverGroups(u, health)) {
      groups.push({ key: `mcp:${g.name}`, name: g.name, server: g, tools: g.tools });
    }
    if (groups.length === 0) return '';
    const count = groups.reduce((sum, g) => sum + g.tools.length, 0);
    const split = groups.reduce(
      (acc, g) => {
        const s = _splitTokens(g);
        return { loaded: acc.loaded + s.loaded, deferred: acc.deferred + s.deferred };
      },
      { loaded: 0, deferred: 0 },
    );
    return html`
      <section>
        <h3>
          Tools
          <span class="sub">
            — ${count} ${count === 1 ? 'tool' : 'tools'} in
            ${groups.length} ${groups.length === 1 ? 'group' : 'groups'} ·
            ${_tokenBits(split.loaded, split.deferred).join(' · ')}
          </span>
        </h3>
        ${groups.map((g) => this._renderToolGroup(g))}
      </section>
    `;
  }

  /**
   * One tool group: a header that answers the summary question on its
   * own, and a body only for the reader who asked for it.
   *
   * The header carries the counts and the health, so collapsing loses
   * nothing but the tool names — which is why collapsed is the default
   * even for the server whose 35 tools are the reason this grouping
   * exists.
   */
  _renderToolGroup(g) {
    const open = this._openGroups.has(g.key);
    const h = g.server?.health || null;
    const { loaded, deferred } = _splitTokens(g);
    const bits = [
      `${g.tools.length} ${g.tools.length === 1 ? 'tool' : 'tools'}`,
      ..._tokenBits(loaded, deferred),
    ];
    return html`
      <div class="group">
        <button
          class="group-head"
          aria-expanded=${open ? 'true' : 'false'}
          @click=${() => this._toggleGroup(g.key)}
        >
          <span class="chev">${open ? '▾' : '▸'}</span>
          <span class="group-name">${g.name}</span>
          ${h
            ? html`<span class="pill" style="color: ${h.color};
                     border-color: ${h.color}">${h.label}</span>`
            : ''}
          <span class="spacer"></span>
          <span class="group-meta">${bits.join(' · ')}</span>
        </button>
        ${open ? this._renderGroupBody(g, h) : ''}
      </div>
    `;
  }

  _renderGroupBody(g, h) {
    return html`
      ${g.server ? this._renderServerDetail(g.server, h) : ''}
      ${g.tools.length === 0
        ? html`<p class="empty">
            This server contributed no tools to the window.
          </p>`
        : html`
            <table>
              <thead>
                <tr>
                  <th>Tool</th>
                  <th class="num">Tokens</th>
                </tr>
              </thead>
              <tbody>
                ${g.tools.map((t) => html`
                  <tr class=${t.deferred ? 'deferred' : ''}>
                    <td>${t.name}</td>
                    <td class="num">${_fmtTokens(t.tokens)}</td>
                  </tr>
                `)}
              </tbody>
            </table>
          `}
    `;
  }

  /**
   * The connection facts behind a server's pill.
   *
   * From `get_mcp_status()`, not from the breakdown: `mcpTools` says what
   * a server costs and nothing about whether it is answering, and a
   * token cost for a server that failed to start is the most misleading
   * row this tab could draw. When that call did not land, the detail says
   * so rather than implying the server is fine.
   */
  _renderServerDetail(server, h) {
    if (!h) {
      return html`<p class="note">
        No connection status for this server — the engine's MCP status
        was not available, so these tokens are the whole picture.
      </p>`;
    }
    const bits = [];
    // `scope` here is the CLI's own word for where the server is
    // configured — project, user, local, managed — and not the
    // settings-key style `agents[].source` uses, so it is shown raw.
    if (h.scope) bits.push(h.scope);
    if (h.transport) bits.push(h.transport);
    if (h.version) bits.push(`v${h.version}`);
    if (h.toolCount != null) {
      bits.push(`${h.toolCount} advertised`);
    }
    return html`
      ${bits.length
        ? html`<p class="note">${bits.join(' · ')}</p>`
        : ''}
      ${h.error ? html`<p class="error">${h.error}</p>` : ''}
      ${this._renderMcpActions(server, h)}
    `;
  }

  /**
   * The two things a host can do to a server, under the facts about it.
   *
   * In the group body rather than on the head, because the head *is* the
   * disclosure `<button>` and a button cannot contain one. The cost is a
   * click to reach a reconnect, which `serverGroups` already softens by
   * sorting an unwell server above a heavier healthy one — the row that
   * needs acting on is the first one in the list, with its state on the
   * closed head.
   *
   * Nothing renders without health, because `_renderServerDetail` has
   * returned by then: with no status there is no way to tell which of
   * these two actions the row wants, and a Disable button on a server that
   * may already be off is worse than no button. Nothing renders for a
   * guest either — both RPCs are localhost-only, so an enabled control
   * would be an offer the engine refuses.
   *
   * Nothing renders for an in-process SDK server either — our own `aic-dc`
   * bridge, which the CLI reports with `scope: "dynamic"`. Measured against
   * CLI 2.1.229 on 2026-08-26, all three calls behave like this:
   *
   *   - `toggle_mcp_server('aic-dc', false)` replies `ok` and takes the
   *     tool count from 6 to 0, while the pill stays `connected`
   *   - `toggle_mcp_server('aic-dc', true)` fails: "SDK servers should be
   *     handled in print.ts"
   *   - `reconnect_mcp_server('aic-dc')` fails the same way
   *
   * So disable is a one-way door on this row: it works, it is the one
   * server whose tools the *agent* runs on, and neither recovery path can
   * undo it — only a new session can. A control the engine will not honour
   * in both directions is not a toggle, so the row states the fact instead
   * of offering the button. This is the only server-kind exception, and it
   * keys off the CLI's own word rather than off the name `aic-dc`, so any
   * other SDK server we register is covered by the same reasoning.
   */
  _renderMcpActions(server, h) {
    if (!this._canControlMcp) return '';
    if (h.scope === 'dynamic') {
      return html`<p class="note">
        Served in-process by this app, so the engine manages it with the
        session — it cannot be switched off and back on from here.
      </p>`;
    }
    const name = server.name;
    const busy = this._mcpBusy.has(name);
    const disabled = busy || !this.rpcConnected;
    const off = h.status === 'disabled';
    const broken = h.status === 'failed' || h.status === 'needs-auth';
    return html`
      <p class="actions">
        ${broken
          ? html`<button
              class="tool-btn"
              ?disabled=${disabled}
              title="Ask the engine to dial ${name} again"
              @click=${() => this._reconnectServer(name)}
            >
              ↻ Reconnect
            </button>`
          : ''}
        <button
          class="tool-btn"
          ?disabled=${disabled}
          title=${off
            ? `Hand ${name}'s tools back to the agent`
            : `Drop ${name}'s tools out of this session`}
          @click=${() => this._toggleServer(name, off, h.toolCount)}
        >
          ${off ? '✓ Enable' : '⊘ Disable'}
        </button>
        ${busy
          ? html`<span class="acting" aria-hidden="true">…</span>`
          : ''}
      </p>
    `;
  }

  /**
   * The subagents this session can delegate to.
   *
   * `source` is a settings scope — which settings file defined the agent
   * — and not a path, so these rows cannot be click-to-open the way
   * memory files are. It is mapped to the CLI's own label rather than
   * shown raw, because "projectSettings" is a key and "Project" is the
   * answer.
   */
  _renderAgents() {
    const agents = (Array.isArray(this._usage.agents) ? this._usage.agents : [])
      .filter((a) => a && typeof a === 'object')
      .sort((a, b) => (Number(b.tokens) || 0) - (Number(a.tokens) || 0));
    if (agents.length === 0) return '';
    const total = agents.reduce((sum, a) => sum + (Number(a.tokens) || 0), 0);
    return html`
      <section>
        <h3>
          Agent definitions
          <span class="sub">
            — ${agents.length} ${agents.length === 1 ? 'agent' : 'agents'},
            ${_fmtTokens(total)} tokens
          </span>
        </h3>
        <table>
          <thead>
            <tr>
              <th>Agent</th>
              <th>Source</th>
              <th class="num">Tokens</th>
            </tr>
          </thead>
          <tbody>
            ${agents.map((a) => html`
              <tr>
                <td>${a.agentType || a.name || '—'}</td>
                <td>${sourceLabel(a.source)}</td>
                <td class="num">${_fmtTokens(a.tokens)}</td>
              </tr>
            `)}
          </tbody>
        </table>
      </section>
    `;
  }

  /**
   * Skills, and the gap between available and loaded.
   *
   * The heading names both counts because they answer different
   * questions: 3 of 40 loaded means 37 skills cost nothing right now,
   * and a reader who sees only "3 skills" will not go looking for the
   * other 37 when the total moves.
   */
  _renderSkills() {
    const inv = skillInventory(this._usage);
    if (!inv) return '';
    const { rows, tokens, total, included } = inv;
    return html`
      <section>
        <h3>
          Skills
          <span class="sub">
            —
            ${included != null && total != null
              ? `${included} of ${total} loaded, `
              : ''}${_fmtTokens(tokens)} tokens
          </span>
        </h3>
        ${rows.length === 0
          ? html`<p class="note">
              The engine reported a total but named no individual skills.
            </p>`
          : html`
              <table>
                <thead>
                  <tr>
                    <th>Skill</th>
                    <th>Source</th>
                    <th class="num">Tokens</th>
                  </tr>
                </thead>
                <tbody>
                  ${rows.map((s) => html`
                    <tr>
                      <td>${s.name}</td>
                      <td>
                        ${s.plugin
                          ? html`${s.source} <span class="note"
                              >(${s.plugin})</span
                            >`
                          : s.source}
                      </td>
                      <td class="num">${_fmtTokens(s.tokens)}</td>
                    </tr>
                  `)}
                </tbody>
              </table>
            `}
      </section>
    `;
  }

  /**
   * Slash commands, which the engine reports only as counts.
   *
   * No per-command rows exist in the payload, so this is one line rather
   * than a table with a single row in it.
   */
  _renderSlashCommands() {
    const sc = this._usage.slashCommands;
    if (!sc || typeof sc !== 'object' || Array.isArray(sc)) return '';
    const total = Number(sc.totalCommands);
    const included = Number(sc.includedCommands);
    const tokens = Number(sc.tokens) || 0;
    if (!Number.isFinite(total) && !Number.isFinite(included) && !tokens) {
      return '';
    }
    return html`
      <section>
        <h3>Slash commands</h3>
        <p class="note">
          ${Number.isFinite(included) && Number.isFinite(total)
            ? `${included} of ${total} in the window`
            : Number.isFinite(total)
              ? `${total} available`
              : 'In the window'}, costing
          ${_fmtTokens(tokens)} tokens.
        </p>
      </section>
    `;
  }

  /**
   * Debug — for diagnosing the engine, not the code.
   *
   * Five readers of five sources, in the order a diagnosis uses them: what
   * binary is running, what it says it can do, what it has been doing,
   * whether its servers are answering, and finally its own layout of the
   * numbers the Usage section lays out itself.
   *
   * Nothing here is required to understand normal usage, which is why it is
   * a section a reader has to ask for rather than a row in the other two.
   */
  _renderDebugSection() {
    return html`
      ${this._renderEngine()}
      ${this._renderServerInfo()}
      ${this._renderHookTraffic()}
      ${this._renderMcpRaw()}
      ${this._renderGridRows()}
    `;
  }

  /**
   * Which `claude` is running, under whose credentials, with what SDK.
   *
   * The four numbers at the top are the ones that explain a whole class of
   * "it worked yesterday": a resolved binary that is not the bundled one, a
   * version below the SDK's pin, a credential source nobody expected.
   *
   * All of it comes from `EngineHealth`, and that is the whole section —
   * these are facts *we* resolved about the engine. What the engine says
   * about itself is the next section, because two tables under one heading
   * lose exactly the provenance a diagnosis needs.
   */
  _renderEngine() {
    const h = this._health;
    const rows = [];
    if (h) {
      rows.push(['Binary', h.cli_path || '—']);
      rows.push([
        'CLI version',
        [h.cli_version || 'unknown', h.cli_source ? `(${h.cli_source})` : '']
          .filter(Boolean)
          .join(' '),
      ]);
      rows.push([
        'SDK',
        [h.sdk_version || 'unknown', h.sdk_cli_pin ? `pins CLI ${h.sdk_cli_pin}` : '']
          .filter(Boolean)
          .join(' · '),
      ]);
      rows.push(['Credentials', h.credential_source || 'unknown']);
      rows.push([
        'Mirror gaps',
        h.mirror_gaps_escalated
          ? `${h.mirror_gaps ?? 0} — past tolerance`
          : String(h.mirror_gaps ?? 0),
      ]);
    }
    return html`
      <section>
        <h3>Engine</h3>
        ${h
          ? html`
              <table>
                <tbody>
                  ${rows.map(([label, value]) => html`
                    <tr>
                      <td>${label}</td>
                      <td class="path">${value}</td>
                    </tr>
                  `)}
                </tbody>
              </table>
              ${h.version_warning
                ? html`<p class="warn">${h.version_warning}</p>`
                : ''}
              ${h.auth_warning ? html`<p class="warn">${h.auth_warning}</p>` : ''}
              ${h.last_error ? html`<p class="error">${h.last_error}</p>` : ''}
              <p class="note">
                The SDK row above says which wheel is installed.
                <button class="link" @click=${this._goToSdkSurface}>
                  Which of its features this build wired up
                </button>
                is the SDK Surface tab (Alt+5) — the same versions, read
                against what <code>aic_dc.claude_code</code> actually sets,
                registers and dispatches.
              </p>
            `
          : html`<p class="note">
              No engine health has arrived yet. It is pushed on connect and
              whenever it changes, so this fills in as soon as the engine
              reports.
            </p>`}
        ${this._renderEngineErrors()}
      </section>
    `;
  }

  /**
   * Engine failures recorded on disk, newest last.
   *
   * Outside the health branch above on purpose. Health is what the
   * *running* engine resolved, so an engine that never started has none —
   * and "never started" is precisely when the reader needs this. Nesting
   * it would have hidden the record in the only case it exists for.
   *
   * Each row carries the credential source and the resolved binary rather
   * than the message alone, because for the failure this was built after —
   * an authentication error on a cold start — which credentials were
   * resolved *is* the diagnosis, and it is the one fact no live surface
   * keeps once the process is gone. The CLI's own stderr goes underneath,
   * preformatted, for the reason the health banner shows it: on a failed
   * connect it is the only account from the thing that actually failed.
   *
   * Silence when the file is empty and has been read, because an engine
   * that has never failed is the ordinary case and a heading over nothing
   * is noise. A read that *failed* says so instead — that distinction is
   * the whole reason the RPC answers a dict rather than a bare list.
   */
  _renderEngineErrors() {
    const state = this._engineErrors;
    if (!state) return '';
    if (state.error) {
      return html`<p class="warn">
        ${state.reason === 'no_repo'
          ? 'Engine failures are not recorded for a run with no repo directory.'
          : state.error}
      </p>`;
    }
    const errors = Array.isArray(state.errors) ? state.errors : [];
    if (errors.length === 0) return '';
    return html`
      <h4>Engine failures</h4>
      <p class="note">
        Read from <code>.aic-dc/engine-errors.jsonl</code>. These outlive the
        server that wrote them, which is the point: a failed start is
        otherwise only a broadcast to whoever was watching.
      </p>
      ${errors.map(
        (e) => html`
          <div class="engine-error">
            <p class="error">${e.message || 'The engine failed to start.'}</p>
            <table>
              <tbody>
                <tr>
                  <td>When</td>
                  <td class="path">${_engineErrorTime(e.timestamp)}</td>
                </tr>
                ${e.credential_source
                  ? html`<tr>
                      <td>Credentials</td>
                      <td class="path">${e.credential_source}</td>
                    </tr>`
                  : ''}
                ${e.cli_path
                  ? html`<tr>
                      <td>Binary</td>
                      <td class="path">
                        ${e.cli_path}${e.cli_version
                          ? ` (${e.cli_version})`
                          : ''}
                      </td>
                    </tr>`
                  : ''}
              </tbody>
            </table>
            ${Array.isArray(e.cli_stderr) && e.cli_stderr.length
              ? html`<pre class="stderr">${e.cli_stderr.join('\n')}</pre>`
              : ''}
          </div>
        `,
      )}
    `;
  }

  /**
   * The initialize reply: summarised by key, then printed verbatim.
   *
   * Its own section rather than a second table under *Engine*, because the
   * provenance is the other way round — everything above is what AIC⚡DC
   * resolved, and everything here is what the engine answered. A diagnosis
   * that cannot tell those apart is reading one table as both.
   */
  _renderServerInfo() {
    return html`
      <section>
        <h3>Initialize reply</h3>
        ${this._renderServerInfoBody()}
      </section>
    `;
  }

  /** The reply's four states: loading, failed, unread, and read. */
  _renderServerInfoBody() {
    if (this._debugLoading && !this._serverInfo) {
      return html`<p class="note">Asking the engine what it advertises…</p>`;
    }
    if (this._debugError) {
      return html`<p class="error">
        Server info unavailable: ${this._debugError}
      </p>`;
    }
    const info = this._serverInfo;
    if (!info) {
      return html`<p class="note">
        Server info has not been read yet.
      </p>`;
    }
    const keys = Object.keys(info);
    if (keys.length === 0) {
      return html`<p class="note">
        The engine answered with an empty record — it advertises nothing, or
        the session has not initialized.
      </p>`;
    }
    return html`
      <table>
        <thead>
          <tr>
            <th>Advertised</th>
            <th>Contents</th>
          </tr>
        </thead>
        <tbody>
          ${keys.map((k) => html`
            <tr>
              <td>${k}</td>
              <td>${_describeValue(info[k])}</td>
            </tr>
          `)}
        </tbody>
      </table>
      <pre class="json">${_json(info)}</pre>
    `;
  }

  /**
   * Which hooks fired, newest first.
   *
   * The log starts when this panel mounts, not when the section opens —
   * otherwise the traffic a reader came to look at is the traffic they just
   * missed. Each row expands to the payload the engine sent, since the
   * summarised columns are the fields we chose to read and the interesting
   * one is usually not among them.
   *
   * What lands here is what the CLI *announces*, which turns out not to be
   * every hook that runs — see the empty state. Verified live in phase 6
   * against CLI 2.1.229.
   */
  _renderHookTraffic() {
    const hooks = this._hooks;
    return html`
      <section>
        <h3>
          Hook traffic
          ${hooks.length
            ? html`<span class="sub">
                — ${hooks.length}
                ${hooks.length === 1 ? 'event' : 'events'}${hooks.length >= _HOOK_LOG_LIMIT
                  ? `, newest ${_HOOK_LOG_LIMIT}`
                  : ''}
              </span>`
            : ''}
        </h3>
        ${hooks.length === 0
          ? html`<p class="note">
              Nothing announced since this panel was mounted. This shows what
              the CLI puts in the message stream, which does not include
              AIC⚡DC's own re-index hook — that one is an SDK callback,
              answered over the control channel. So an empty table is the
              normal state here, and it is not evidence that the re-index
              did not run.
            </p>`
          : html`
              <table>
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Hook</th>
                    <th>Tool</th>
                    <th>Outcome</th>
                  </tr>
                </thead>
                <tbody>
                  ${hooks.map((e) => this._renderHookRows(e))}
                </tbody>
              </table>
            `}
      </section>
    `;
  }

  _renderHookRows(e) {
    const key = `hook:${e.id}`;
    const open = this._openGroups.has(key);
    const outcome = [
      e.outcome,
      e.exitCode != null && e.exitCode !== 0 ? `exit ${e.exitCode}` : '',
    ]
      .filter(Boolean)
      .join(' · ');
    return html`
      <tr>
        <td>${new Date(e.at).toLocaleTimeString()}</td>
        <td>
          <button
            class="link"
            aria-expanded=${open ? 'true' : 'false'}
            title="Show the payload the engine sent"
            @click=${() => this._toggleGroup(key)}
          >${e.name}</button>
          ${e.phase && e.phase !== e.name
            ? html` <span class="note">(${e.phase})</span>`
            : ''}
        </td>
        <td>${e.tool || '—'}</td>
        <td class=${e.exitCode ? 'error' : ''}>${outcome || '—'}</td>
      </tr>
      ${open
        ? html`<tr>
            <td colspan="4"><pre class="json">${_json(e.raw)}</pre></td>
          </tr>`
        : ''}
    `;
  }

  /**
   * `get_mcp_status()` verbatim.
   *
   * The Session section already renders this through `mcpHealth`, which is
   * the point of printing it raw here: the two together are how a reader
   * checks our reading of a payload against the payload.
   */
  _renderMcpRaw() {
    return html`
      <section>
        <h3>MCP status</h3>
        ${this._mcpStatus
          ? html`<pre class="json">${_json(this._mcpStatus)}</pre>`
          : html`<p class="note">
              The last status fetch did not land, so the Session section's
              server pills are absent too.
            </p>`}
      </section>
    `;
  }

  /**
   * The CLI's own pre-laid-out grid, printed and never rendered.
   *
   * It is a terminal's layout decision. Laying the tab out from it would
   * couple this view to a presentation choice that can change under us, so
   * the Usage section lays out from `categories` and this is where the two
   * can be compared.
   */
  _renderGridRows() {
    const grid = this._usage?.gridRows;
    return html`
      <section>
        <h3>Grid rows</h3>
        <p class="note">
          The CLI's own layout of these numbers, for cross-checking the Usage
          section against it. Never used for layout here.
        </p>
        ${grid
          ? html`<pre class="json">${_json(grid)}</pre>`
          : html`<p class="note">
              This breakdown carried no grid rows.
            </p>`}
      </section>
    `;
  }

  _renderFooter() {
    if (this._error) {
      // A failed refresh with usable prior numbers: say so rather than
      // leaving stale figures looking current.
      return html`<p class="error">
        Last refresh failed: ${this._error}
      </p>`;
    }
    if (!this._fetchedAt) return '';
    return html`<p class="note">
      Read from the engine at ${this._formatFetchedAt()}.
    </p>`;
  }

  _formatFetchedAt() {
    const d = new Date(this._fetchedAt);
    if (Number.isNaN(d.getTime())) return this._fetchedAt;
    return d.toLocaleTimeString();
  }
}

customElements.define('aic-context-usage-tab', ContextUsageTab);
