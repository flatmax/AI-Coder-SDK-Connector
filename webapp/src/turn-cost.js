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
//
// `turn_model_usage` has two producers, both per-turn and therefore both
// under that one name: the `streamComplete` the engine builds at the end
// of a turn, and the `turnUsage` it pushes *during* one, as each
// assistant message reports what it used (`ac_dc/claude_code/messages.py`).
// `modelUsageLines` reads either. The live one is a running figure —
// incomplete until the result lands, and replaced by it then — but it is
// never a session total, which is the only distinction this module
// polices.

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

function _num(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** A cost we are willing to print. Negative is a bug upstream, not a refund. */
function _cost(value) {
  const n = _num(value);
  return n !== null && n >= 0 ? n : null;
}

/**
 * One usage entry's four counters, plus the sums worth naming.
 *
 * Each counter is read camelCase first. The engine sends camelCase
 * (`inputTokens`) because that is what the CLI's wire schema uses, and a
 * replayed transcript sends snake_case (`input_tokens`) because that is
 * what the CLI writes to disk. Reading only one style is how live turns
 * came to render no per-model lines at all while browsed ones rendered
 * fine.
 *
 * The three input-side counters are separate because they are priced an
 * order of magnitude apart — a cache read is a fraction of full price, a
 * cache write is a premium over it — and because `input` is **only the
 * uncached remainder of the prompt**, not the prompt. An agentic turn
 * resends its whole context on every step, so nearly all of a long
 * turn's prompt is a cache read, and `input` on its own reports a
 * fraction of what was sent: "3k in" for a turn that actually submitted
 * 50k is the wrong number, not a rounded one. `prompt` is the honest
 * total of what went up; `cache` is the two cache counters together, for
 * a reader who wants three numbers rather than four.
 *
 * Missing counters read as 0 and negatives are ignored, on the same
 * grounds as a negative cost: a bug upstream, not a credit.
 *
 * @param {Object} usage  a per-model usage entry, either spelling
 * @returns {{input: number, output: number, cacheCreation: number,
 *            cacheRead: number, cache: number, prompt: number,
 *            total: number}}
 */
export function usageSplit(usage) {
  const entry = usage && typeof usage === 'object' ? usage : null;
  const read = (camel, snake) => {
    if (entry === null) return 0;
    const value = _num(entry[camel]) ?? _num(entry[snake]);
    return value !== null && value > 0 ? value : 0;
  };
  const input = read('inputTokens', 'input_tokens');
  const output = read('outputTokens', 'output_tokens');
  const cacheCreation = read(
    'cacheCreationInputTokens', 'cache_creation_input_tokens',
  );
  const cacheRead = read('cacheReadInputTokens', 'cache_read_input_tokens');
  return {
    input,
    output,
    cacheCreation,
    cacheRead,
    cache: cacheCreation + cacheRead,
    prompt: input + cacheCreation + cacheRead,
    total: input + output + cacheCreation + cacheRead,
  };
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
  return usageSplit(usage).total;
}

/**
 * What one subagent spent, from the SDK's `TaskUsage`.
 *
 * A separate reader because `TaskUsage` shares no field names with the
 * per-model counters above — it is `{total_tokens, tool_uses, duration_ms}`,
 * already summed by the CLI — so `turnTokens` returns 0 for every one of them.
 * That is how the subagent row's "N tok" chip came to be permanently absent
 * and the completed-LED tooltip had no counters to name.
 *
 * `tokens` falls back to `turnTokens` so a payload carrying per-model counters
 * instead (a transcript listing, a future shape) still reports something.
 * Zero means "not reported": callers drop the figure rather than print it,
 * because "0 tokens" is a claim about a subagent that demonstrably ran.
 *
 * @param {Object} usage  a `TaskUsage`-shaped payload
 * @returns {{tokens: number, toolUses: number, durationMs: number}}
 */
export function taskUsage(usage) {
  if (!usage || typeof usage !== 'object') {
    return { tokens: 0, toolUses: 0, durationMs: 0 };
  }
  const total = _num(usage.total_tokens) ?? _num(usage.totalTokens);
  const uses = _num(usage.tool_uses) ?? _num(usage.toolUses);
  const duration = _num(usage.duration_ms) ?? _num(usage.durationMs);
  return {
    tokens: total !== null && total > 0 ? total : turnTokens(usage),
    toolUses: uses !== null && uses > 0 ? uses : 0,
    durationMs: duration !== null && duration > 0 ? duration : 0,
  };
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
 * Each line carries the split as well as the total, because one number
 * cannot say whether a turn's tokens were prompt or completion and the
 * two differ by 5× in price. See `usageSplit` for why the input side is
 * three numbers rather than one.
 *
 * @param {Object} result  a `streamComplete` or `turnUsage` payload
 * @returns {Array<{model: string, tokens: number, usd: number|null,
 *                  input: number, output: number, cacheRead: number,
 *                  cacheCreation: number, cache: number, prompt: number}>}
 */
export function modelUsageLines(result) {
  const usage = result?.turn_model_usage;
  if (!usage || typeof usage !== 'object') return [];
  const lines = [];
  for (const [key, entry] of Object.entries(usage)) {
    if (!entry || typeof entry !== 'object') continue;
    const split = usageSplit(entry);
    if (split.total <= 0) continue;
    const canonical = entry.canonicalModel;
    lines.push({
      model: typeof canonical === 'string' && canonical ? canonical : String(key),
      tokens: split.total,
      input: split.input,
      output: split.output,
      cacheRead: split.cacheRead,
      cacheCreation: split.cacheCreation,
      cache: split.cache,
      prompt: split.prompt,
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
