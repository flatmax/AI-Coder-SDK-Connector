// Queue ordering and default-focus selection — the parts of the
// permission dialog that are pure functions of a payload.
//
// They live here rather than in the component because each one encodes
// an invariant from specs5/5-webapp/permission-dialog.md that is worth
// testing without a DOM: what order requests are answered in, and which
// control the keyboard lands on.

import {
  AMBER_SECONDS,
  CLASS_LABELS,
  DEFAULT_DENY_REASONS,
  DESTINATION_FILES,
  RED_SECONDS,
  RISKY_FLAGS,
} from './constants.js';

/**
 * Parse an ISO `expires_at` to epoch milliseconds.
 *
 * Returns `Infinity` for anything unparseable, so a malformed timestamp
 * sorts last and renders no countdown rather than sorting first and
 * jumping the queue.
 *
 * @param {string|number|null|undefined} value
 * @returns {number}
 */
export function expiryMs(value) {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value !== 'string' || !value) return Infinity;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : Infinity;
}

/**
 * Order the queue: soonest expiry first, arrival order breaking ties.
 *
 * Answering the request closest to timing out first is the only ordering
 * that does not silently lose one to the clock while the user reads
 * another (permission-dialog.md § Queue).
 *
 * `arrivedAt` is a monotonic counter the caller assigns, not a
 * timestamp — two requests can arrive in the same millisecond.
 *
 * @param {Array<{payload: object, arrivedAt: number}>} entries
 * @returns {Array<{payload: object, arrivedAt: number}>} a new array
 */
export function orderQueue(entries) {
  return [...entries].sort((a, b) => {
    const left = expiryMs(a.payload?.expires_at);
    const right = expiryMs(b.payload?.expires_at);
    if (left !== right) return left - right;
    return (a.arrivedAt ?? 0) - (b.arrivedAt ?? 0);
  });
}

/**
 * Which decision control default focus lands on after the settling
 * interval.
 *
 * Deny for `mcp`, and for `exec` calls carrying an advisory `deletes` or
 * `network` flag. Allow for everything else. The controls themselves
 * stay in fixed positions — it is the *focus* that moves with risk, so
 * muscle memory alone cannot approve a risky call
 * (permission-dialog.md § Anti-Click-Through, points 2 and 3).
 *
 * @param {object|null} payload
 * @returns {'allow'|'deny'}
 */
export function defaultFocusTarget(payload) {
  if (!payload) return 'deny';
  if (payload.tool_class === 'mcp') return 'deny';
  if (payload.tool_class === 'interact') return 'allow';
  const flags = payload.command?.flags;
  if (Array.isArray(flags) && flags.some((flag) => RISKY_FLAGS.includes(flag))) {
    return 'deny';
  }
  return 'allow';
}

/**
 * Seconds remaining until expiry, floored at zero.
 *
 * Derived from `expires_at` and the wall clock rather than a timer
 * started on arrival, so a slow socket cannot produce a dialog claiming
 * more time than it has (permission-dialog.md § Countdown and Timeout).
 *
 * @param {object|null} payload
 * @param {number} now — epoch milliseconds
 * @returns {number|null} null when there is no parseable expiry
 */
export function secondsRemaining(payload, now) {
  const expiry = expiryMs(payload?.expires_at);
  if (!Number.isFinite(expiry)) return null;
  return Math.max(0, Math.ceil((expiry - now) / 1000));
}

/**
 * `m:ss`, or `0:0n` under ten seconds. Never hidden, so it always
 * formats to something.
 *
 * @param {number|null} seconds
 * @returns {string}
 */
export function formatCountdown(seconds) {
  if (seconds == null) return '—';
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${minutes}:${String(rest).padStart(2, '0')}`;
}

/**
 * Urgency class for the countdown. Carries the numeral too, so colour
 * is never the only signal (permission-dialog.md § Accessibility).
 *
 * @param {number|null} seconds
 * @returns {''|'amber'|'red'}
 */
export function countdownUrgency(seconds) {
  if (seconds == null) return '';
  if (seconds < RED_SECONDS) return 'red';
  if (seconds < AMBER_SECONDS) return 'amber';
  return '';
}

/**
 * The screen-reader announcement made on arrival.
 *
 * Class, tool, target, and the diff stats where there are any — rather
 * than walking the reader through the diff itself
 * (permission-dialog.md § Accessibility).
 *
 * @param {object|null} payload
 * @returns {string}
 */
export function arrivalAnnouncement(payload) {
  if (!payload) return '';
  const label = CLASS_LABELS[payload.tool_class] || payload.tool_class || 'tool';
  const parts = [`permission request: ${label}`];
  const diff = payload.diff;
  if (diff?.path) {
    parts.push(diff.path);
    if (diff.is_new_file) {
      parts.push('new file');
    } else if (diff.is_binary) {
      parts.push('binary, cannot be shown');
    } else if (diff.too_large) {
      parts.push('too large to diff');
    } else {
      parts.push(`${diff.additions} added ${diff.deletions} removed`);
    }
  } else if (payload.command?.command) {
    parts.push(payload.command.command.slice(0, 200));
  } else if (payload.question?.question) {
    parts.push(payload.question.question);
  } else if (payload.summary) {
    parts.push(payload.summary);
  }
  if (payload.agent_id) parts.push('requested by a subagent');
  return parts.join(', ');
}

/**
 * The header's identifying line: the one piece of the input that says
 * *which* call this is.
 *
 * Prefers the CLI's own `title` where it has one, because that is what
 * the terminal would show for the same call.
 *
 * @param {object|null} payload
 * @returns {string}
 */
export function headerTarget(payload) {
  if (!payload) return '';
  if (payload.diff?.path) return payload.diff.path;
  if (payload.command?.command) {
    const single = payload.command.command.replace(/\s+/g, ' ').trim();
    return single.length > 90 ? `${single.slice(0, 89)}…` : single;
  }
  if (payload.server) return `${payload.server} › ${payload.tool_name}`;
  if (payload.title) return payload.title;
  return payload.summary || payload.tool_name || '';
}

/**
 * The prefilled deny reason for a request. Editable; never empty.
 *
 * @param {object|null} payload
 * @returns {string}
 */
export function defaultDenyReason(payload) {
  return DEFAULT_DENY_REASONS[payload?.tool_class]
    || 'The user denied this call.';
}

/**
 * Label a suggested rule for its button: the rule text itself, plus
 * where it will be written, plus whether we guessed it.
 *
 * The button says what will happen to a file on disk. "Always allow"
 * with no rule text is the promise the spec forbids
 * (permission-dialog.md § Always allow shows the rule, not a promise).
 *
 * @param {object|null} rule
 * @returns {{label: string, destination: string, derived: boolean,
 *   session: boolean, shared: boolean}|null}
 */
export function describeRule(rule) {
  if (!rule || !rule.tool_name) return null;
  const target = rule.rule_content ? `(${rule.rule_content})` : '';
  const verb = rule.behavior === 'deny'
    ? 'Always deny'
    : rule.behavior === 'ask' ? 'Always ask about' : 'Always allow';
  return {
    label: `${verb} ${rule.tool_name}${target}`,
    destination: DESTINATION_FILES[rule.destination] || rule.destination || '',
    derived: rule.origin !== 'cli',
    // The one entry that writes to the git-tracked file (CC-16). The
    // destination chip alone does not carry it: two menu rows differing only
    // by `.local` in a filename is a distinction a person reading quickly
    // will not make, and the consequence — the grant reaching everyone who
    // pulls — is not recoverable by unclicking.
    shared: rule.shared === true,
    // Session grants are not written anywhere, so the tooltip has to
    // promise something different. The CLI suggests this destination for
    // reads outside the working directory, so it is a normal case rather
    // than an edge one.
    session: rule.destination === 'session',
  };
}

/**
 * Whether the "always allow" control is offered at all.
 *
 * Never for `interact`: there is no rule that can answer a future
 * question (permission-dialog.md § interact).
 *
 * @param {object|null} payload
 * @returns {boolean}
 */
export function offersAlwaysAllow(payload) {
  if (!payload || payload.tool_class === 'interact') return false;
  return Array.isArray(payload.suggested_rules)
    && payload.suggested_rules.length > 0;
}

/**
 * Describe the mode switch the CLI offered for this call, if any.
 *
 * The copy comes from the engine, which is the side that knows which modes
 * it is willing to offer and what each one costs. Splitting that between
 * the two sides would let a button describe a consequence the engine does
 * not actually apply, so anything without both a mode and a label is not
 * rendered at all.
 *
 * Never for `interact`, for the same reason `offersAlwaysAllow` is not:
 * there is no standing grant that answers a future question.
 *
 * @param {object|null} payload
 * @returns {{mode: string, label: string, detail: string, destination: string}|null}
 */
export function describeMode(payload) {
  if (!payload || payload.tool_class === 'interact') return null;
  const offer = payload.suggested_mode;
  if (!offer || typeof offer.mode !== 'string' || !offer.mode) return null;
  if (typeof offer.label !== 'string' || !offer.label) return null;
  return {
    mode: offer.mode,
    label: offer.label,
    detail: typeof offer.detail === 'string' ? offer.detail : '',
    destination: DESTINATION_FILES[offer.destination]
      || offer.destination || '',
  };
}

/**
 * Every question an `interact` request is asking, in order.
 *
 * `AskUserQuestion` takes up to four questions in one call. The engine
 * promotes the first to `question.question` / `question.options` so a
 * dialog that renders one is still correct, and carries the whole list
 * in `question.questions`. Falling back to the promoted fields keeps a
 * payload that predates the list — or a hand-built one — renderable.
 *
 * @param {object|null} payload
 * @returns {Array<object>}
 */
export function interactQuestions(payload) {
  const question = payload?.question;
  if (!question) return [];
  if (Array.isArray(question.questions) && question.questions.length) {
    return question.questions;
  }
  return question.question ? [question] : [];
}

/**
 * Whether every question has at least one option selected.
 *
 * The agent is waiting on all of them: an answer map missing a key reads
 * to the CLI as a question the user declined to answer, so "Answer"
 * stays disabled until the set is complete rather than sending a partial
 * reply the agent cannot tell from a refusal.
 *
 * @param {object|null} payload
 * @param {Map<number, Set<number>>} answers — question index → chosen options
 * @returns {boolean}
 */
export function answersComplete(payload, answers) {
  const questions = interactQuestions(payload);
  if (!questions.length) return false;
  return questions.every((_question, index) => (answers.get(index)?.size ?? 0) > 0);
}
