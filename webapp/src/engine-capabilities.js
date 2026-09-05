// Which surfaces the running engine can feed — the browser half of the
// capability descriptor.
//
// Governing spec: specs5/plan-ag/decisions.md AG-3 and AG-9.
//
// The server publishes `ClaudeCodeService.get_engine_capabilities()`, a map
// of surface key → `{supported, status, note}`. This module fetches it once
// and answers `supports(key)` synchronously, so a render path can ask without
// awaiting.
//
// Why hiding rather than drawing a zero
// -------------------------------------
// An empty list does not say "no servers", it says "no answer" — the lesson
// the deleted `EngineHealth.mcp` field left behind. A Context tab drawing a
// 0% bar for an engine that cannot report its context window is worse than one
// that draws no bar at all, because the first is a measurement and the second
// is an absence. A number on screen is believed.
//
// The rule this module exists to enforce (AG-R-4)
// ----------------------------------------------
// **No component may branch on an engine name.** The descriptor deliberately
// carries no engine identity — no `engine`, `name` or `adapter` field at any
// level — so there is nothing to switch on even if somebody tried. Ask
// `supports('usd_cost')`, never `engine === 'antigravity'`. A capability is a
// fact about what can be rendered; an engine name is a guess about it that
// goes stale the moment a third engine or a new SDK release arrives.
//
// The default before the answer arrives
// -------------------------------------
// `supports()` answers **true** while the descriptor is still loading, and
// that default is chosen rather than fallen into.
//
//   - Answering `false` would hide every panel for the width of one RPC round
//     trip on the *shipped* engine, which is a visible regression for the
//     common case in exchange for tidiness in the rare one.
//   - Answering `true` renders a panel that may then hide. Every reader here
//     already tolerates absent data — they were written for an engine that had
//     not connected yet — so the cost is a panel that empties, not one that
//     breaks.
//
// And the failure is caught on the server side regardless: a method serving a
// surface this engine cannot feed raises `UnsupportedOnThisEngine` rather than
// returning a plausible empty value, so a fetch that slips through during load
// fails loudly instead of drawing a synthesised zero.

/**
 * Surface keys, mirroring `src/aic_dc/capabilities.py`.
 *
 * Named rather than passed as free strings so a typo is a broken import at
 * build time instead of a silently hidden panel at run time. The server
 * raises on an unknown key for the same reason.
 */
export const SURFACE = Object.freeze({
  ACCOUNT_RATE_LIMITS: 'account_rate_limits',
  USD_COST: 'usd_cost',
  CONTEXT_WINDOW_USAGE: 'context_window_usage',
  SLASH_COMMANDS: 'slash_commands',
  PERSISTED_PERMISSION_RULES: 'persisted_permission_rules',
  AMEND_TOOL_INPUT: 'amend_tool_input',
  MCP_SERVER_INVENTORY: 'mcp_server_inventory',
  SESSION_MIRROR: 'session_mirror',
  TRANSCRIPT_HISTORY: 'transcript_history',
  SESSION_FORK: 'session_fork',
  RATE_LIMIT_EVENTS: 'rate_limit_events',
  SUBAGENT_TABS: 'subagent_tabs',
  AGENT_QUESTIONS: 'agent_questions',
  FILE_CHECKPOINTING: 'file_checkpointing',
  IMAGE_GENERATION: 'image_generation',
});

/** The descriptor, or null until it has been fetched. */
let _descriptor = null;

/** The in-flight fetch, so concurrent callers share one round trip. */
let _pending = null;

/**
 * Whether the running engine can feed `key`.
 *
 * @param {string} key — one of {@link SURFACE}
 * @returns {boolean} — true when supported, and true when not yet known
 */
export function supports(key) {
  if (!_descriptor) return true;
  const entry = _descriptor[key];
  // An unknown key reads as supported rather than hidden, which is the
  // opposite of the server's rule and deliberately so: there, an unknown key
  // is a programming error worth raising on; here it is most likely a webapp
  // built against a newer server, and hiding a panel over a version skew is a
  // worse outcome than showing one whose data may be empty.
  if (!entry) return true;
  return entry.supported !== false;
}

/**
 * Why a surface is unavailable, for a diagnostics view.
 *
 * `'absent'` — no source data exists and none will. `'unbuilt'` — the data
 * exists and nothing reads it yet. The distinction is never rendered as a
 * user-facing excuse where a panel used to be; that would replace a
 * measurement with an apology. It is for the Debug section.
 *
 * @param {string} key
 * @returns {{status: string, note: string, title: string}|null}
 */
export function surfaceDetail(key) {
  if (!_descriptor) return null;
  return _descriptor[key] || null;
}

/** The whole descriptor, or null. For the Debug section. */
export function descriptor() {
  return _descriptor;
}

/** True once the answer is real rather than the loading default. */
export function isLoaded() {
  return _descriptor !== null;
}

/**
 * Fetch the descriptor once and cache it.
 *
 * Safe to call from every component's `connectedCallback`: concurrent callers
 * share the one in-flight promise, and later calls resolve immediately from
 * the cache.
 *
 * Never throws. An engine that cannot answer leaves the loading default in
 * place, which is today's behaviour on the shipped engine — the descriptor is
 * how a panel learns to hide, so failing to fetch it must not hide anything.
 *
 * @param {{rpcExtract: Function}} host — any component with the RPC mixin
 * @returns {Promise<object|null>}
 */
export async function loadCapabilities(host) {
  if (_descriptor) return _descriptor;
  if (_pending) return _pending;
  _pending = (async () => {
    try {
      const result = await host.rpcExtract(
        'ClaudeCodeService.get_engine_capabilities',
      );
      if (result && typeof result === 'object' && !result.error) {
        _descriptor = result;
      }
    } catch (err) {
      // Left null on purpose. See the docstring: a missing descriptor must
      // not hide anything, so this degrades to "everything supported".
      //
      // `debug` rather than `warn`, because this is a normal state and not
      // a fault: a component can ask before the RPC proxy is published, and
      // the consequence — every surface renders — is exactly the behaviour
      // of the engine that ships today. Warning here would print a stack on
      // every disconnected render and train the reader to ignore the
      // console, which costs more than it catches.
      console.debug('Capability descriptor not readable yet:', err);
    } finally {
      _pending = null;
    }
    return _descriptor;
  })();
  return _pending;
}

/**
 * Drop the cache so the next `loadCapabilities` refetches.
 *
 * Called when the engine changes underneath the session — a restart, or a
 * different master — because the descriptor is a property of the running
 * engine and a stale one would hide the wrong panels. Also what the tests
 * use to isolate.
 */
export function resetCapabilities() {
  _descriptor = null;
  _pending = null;
}

/**
 * Install a descriptor directly, for tests and for a server push.
 *
 * @param {object|null} value
 */
export function setCapabilities(value) {
  _descriptor = value && typeof value === 'object' ? value : null;
  _pending = null;
}

/**
 * Surfaces the running engine reports with a given `status`.
 *
 * The webapp hides `absent` and `unbuilt` identically — that is AG-9, and
 * it is right, because *why* a panel has no data is not the panel's
 * business. This reader exists for the one place the distinction is the
 * whole point: telling somebody why the application they are looking at
 * is missing features it had yesterday.
 *
 * `unbuilt` is the honest answer to that question and `absent` is not. An
 * absent surface is a real difference between two engines — the running
 * one has no USD figure and never will — and the UI is complete without
 * it. An unbuilt one is a feature this project has built, on an engine
 * that cannot yet reach it, and its absence is a half-finished
 * application rather than a design decision. Only the second is worth
 * interrupting somebody about.
 *
 * Returns titles rather than keys because the caller renders them: the
 * descriptor's `title` is the surface's user-facing name, written once on
 * the server so that the browser is not a second author of what the
 * surface is called. Empty before the descriptor has loaded, which reads
 * correctly — nothing is known to be missing yet.
 *
 * @param {string} status — `'supported'`, `'absent'` or `'unbuilt'`
 * @returns {Array<{key: string, title: string, note: string}>} sorted by title
 */
export function surfacesWithStatus(status) {
  if (!_descriptor) return [];
  return Object.entries(_descriptor)
    .filter(([, entry]) => entry && entry.status === status)
    .map(([key, entry]) => ({
      key,
      title: entry.title || key,
      note: entry.note || '',
    }))
    .sort((a, b) => a.title.localeCompare(b.title));
}
