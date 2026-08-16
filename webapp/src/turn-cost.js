// Shared derivations over what a turn cost.
//
// Two components report a turn's price — the usage HUD and the chat
// panel's turn footer — and they disagreed about every part of it. The
// arithmetic and the wording live here once, for the same reason
// context-usage.js exists.
//
// Governing specs: specs5/5-webapp/viewers-hud.md § Usage HUD (CC-17),
// specs5/3-engine/context-visibility.md.
//
// The thing both components had wrong: `total_cost_usd` and
// `model_usage` on a `streamComplete` are **session running totals**, not
// this turn's. The CLI's own wire schema says so in as many words —
// "cumulative across turns in streaming-input sessions: each result
// carries the running total so far, so read the latest result rather
// than summing across results" — and AC⚡DC runs exactly one
// streaming-input client. So the HUD's "This turn · $1.87" was the whole
// session's spend, and its model list named every model the session had
// ever touched, not the ones that answered.
//
// The per-turn figures are a difference against the previous result, so
// the baseline is session state and the engine takes the difference
// server-side (`ac_dc/claude_code/cost.py`). Everything here reads what
// it produced:
//
//   turn_cost_usd     this turn's cost, or null
//   turn_cost_basis   why it is null when it is
//   turn_model_usage  per-model counters for this turn only
//
// One name per scope, so a renderer cannot read a cumulative figure as a
// per-turn one: `model_usage`/`total_cost_usd` are always the engine's
// running totals, and nothing in this module touches them.

/** A difference in hand. Zero is an answer: the turn cost nothing extra. */
export const MEASURED = 'measured';
/** The engine's total went backwards — a `/clear`, or a resumed session. */
export const RESET = 'reset';
/** No usable number: a footer AC⚡DC wrote itself, or one the CLI zeroed. */
export const UNPRICED = 'unpriced';
/**
 * Nothing on the payload says anything about cost at all.
 *
 * A browsed turn: cost is not in the CLI's transcript, so a replayed
 * footer has no basis field. Distinct from `unpriced` on purpose —
 * "never recorded" is not "we lost track of it", and only the second is
 * worth telling the user about.
 */
export const UNRECORDED = 'unrecorded';

const _BASES = new Set([MEASURED, RESET, UNPRICED]);

/**
 * Token counters, camelCase first.
 *
 * The engine sends camelCase (`inputTokens`) because that is what the
 * CLI's wire schema uses, and a replayed transcript sends snake_case
 * (`input_tokens`) because that is what the CLI writes to disk. Reading
 * only one style is how live turns came to render no per-model lines at
 * all while browsed ones rendered fine.
 */
const _COUNTERS = [
  ['inputTokens', 'input_tokens'],
  ['outputTokens', 'output_tokens'],
  ['cacheCreationInputTokens', 'cache_creation_input_tokens'],
  ['cacheReadInputTokens', 'cache_read_input_tokens'],
];

function _num(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** A cost we are willing to print. Negative is a bug upstream, not a refund. */
function _cost(value) {
  const n = _num(value);
  return n !== null && n >= 0 ? n : null;
}

/**
 * Every token a usage entry accounts for.
 *
 * Cache reads and writes are included: they are tokens the turn moved,
 * and a summary that omitted them would show a large cached turn as
 * tiny. Returns 0 for a missing or empty entry, which callers read as
 * "nothing to show" rather than "a free turn".
 */
export function turnTokens(usage) {
  if (!usage || typeof usage !== 'object') return 0;
  let total = 0;
  for (const [camel, snake] of _COUNTERS) {
    const value = _num(usage[camel]) ?? _num(usage[snake]);
    if (value !== null && value > 0) total += value;
  }
  return total;
}

/**
 * What this turn cost, and how much faith to put in it.
 *
 * @param {Object} result  a `streamComplete` payload
 * @returns {{usd: number|null, basis: string}}
 */
export function turnCost(result) {
  if (!result || typeof result !== 'object') return { usd: null, basis: UNRECORDED };
  const basis = result.turn_cost_basis;
  if (typeof basis !== 'string' || !_BASES.has(basis)) {
    return { usd: null, basis: UNRECORDED };
  }
  if (basis !== MEASURED) return { usd: null, basis };
  const usd = _cost(result.turn_cost_usd);
  // A basis of `measured` with no number behind it is a contradiction.
  // Trust the number, not the label: printing nothing beats printing a
  // figure we cannot source.
  return usd === null ? { usd: null, basis: UNPRICED } : { usd, basis: MEASURED };
}

/**
 * Format a USD cost, in the CLI's own format.
 *
 * Four decimals up to fifty cents, two above — lifted from the format
 * function in the bundled `claude` binary so a figure here reads the
 * same as the one the terminal shows. Per-turn costs are mostly
 * fractions of a cent, and two decimals would render nearly every turn
 * as "$0.00", which reads as free rather than as small.
 */
export function formatCost(usd) {
  const n = _cost(usd);
  if (n === null) return null;
  return n > 0.5 ? `$${n.toFixed(2)}` : `$${n.toFixed(4)}`;
}

/**
 * How to render the turn's cost: the text, and the tooltip that says why.
 *
 * Returns null when there is nothing to say at all, which callers render
 * as an absent chip rather than an empty one.
 *
 * The three non-null cases are the distinction phase 6 exists to draw. A
 * turn that cost nothing extra and a turn whose cost is unknown used to
 * render identically — as the word "included", which additionally
 * claimed a billing mode the payload says nothing about.
 *
 * @param {Object} result  a `streamComplete` payload
 * @returns {{text: string, title: string, known: boolean}|null}
 */
export function costLabel(result) {
  const { usd, basis } = turnCost(result);
  if (basis === MEASURED && usd > 0) {
    return {
      text: formatCost(usd),
      title:
        `What this turn added to the session's cost, `
        + `now ${formatCost(_cost(result?.total_cost_usd)) ?? 'unreported'} in total. `
        + 'An estimate the engine computes, not a billing statement.',
      known: true,
    };
  }
  if (basis === MEASURED) {
    return {
      text: 'nothing extra',
      title:
        "The session's cost estimate did not move for this turn — it was "
        + 'served entirely from what had already been paid for. Not a claim '
        + 'about your billing plan.',
      known: true,
    };
  }
  if (basis === RESET) {
    return {
      text: 'cost unknown',
      title:
        "The engine's running cost total restarted during this turn (a "
        + '/clear, or a resumed session), so this turn\'s share of it cannot '
        + 'be separated out. The next turn is priced normally.',
      known: false,
    };
  }
  if (basis === UNPRICED) {
    return {
      text: 'cost unknown',
      title:
        'The turn ended without a usable cost figure, so what it spent is '
        + 'not lost — it lands on the next turn the engine prices. A turn '
        + 'that fails late has usually spent real money.',
      known: false,
    };
  }
  return null;
}

/**
 * Per-model lines for this turn, largest first.
 *
 * Reads `turn_model_usage` and nothing else. Falling back to the
 * engine's cumulative `model_usage` would put the session's totals on a
 * line labelled with this turn's — which is the misreading this module
 * was written to end.
 *
 * The display name prefers `canonicalModel`: the map's key is the raw
 * model string the provider reported, which on Bedrock or Vertex is an
 * id like `us.anthropic.claude-opus-5-v1:0`, and the schema carries the
 * canonical name beside it. (It does *not* carry a `modelName` — the
 * field this code used to prefer never existed.)
 *
 * @param {Object} result  a `streamComplete` payload
 * @returns {Array<{model: string, tokens: number, usd: number|null}>}
 */
export function modelUsageLines(result) {
  const usage = result?.turn_model_usage;
  if (!usage || typeof usage !== 'object') return [];
  const lines = [];
  for (const [key, entry] of Object.entries(usage)) {
    if (!entry || typeof entry !== 'object') continue;
    const tokens = turnTokens(entry);
    if (tokens <= 0) continue;
    const canonical = entry.canonicalModel;
    lines.push({
      model: typeof canonical === 'string' && canonical ? canonical : String(key),
      tokens,
      usd: _cost(entry.costUSD),
    });
  }
  return lines.sort((a, b) => b.tokens - a.tokens);
}

/** The models that answered this turn, busiest first. */
export function modelNames(result) {
  return modelUsageLines(result).map((line) => line.model);
}

/**
 * Whether a failed turn has anything worth popping the HUD for.
 *
 * The rule used to be simpler — no HUD for an errored turn at all —
 * which hid the cost of a turn that failed *late*, after the model had
 * answered and the tools had run. That makes an expensive failure look
 * free, and it is the one case the HUD most needs to report.
 *
 * A turn that died before any of that (a connect failure, a lost socket
 * on the first message) still gets nothing: there is no number, and the
 * chat panel plus a toast already carry the error.
 *
 * The last two checks are what make a crash footer legible. The engine
 * never wrote it — AC⚡DC did — so it carries no usage at all, and the
 * only evidence the turn spent money is that it had already done work.
 */
export function reportsUsage(result) {
  if (!result || typeof result !== 'object') return false;
  const { usd, basis } = turnCost(result);
  if (basis === MEASURED && usd > 0) return true;
  if (modelUsageLines(result).some((line) => line.tokens > 0)) return true;
  const toolCalls = _num(result.tool_calls);
  if (toolCalls !== null && toolCalls > 0) return true;
  return typeof result.response === 'string' && result.response.trim() !== '';
}
