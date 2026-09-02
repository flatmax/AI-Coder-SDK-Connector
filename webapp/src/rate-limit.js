// Shared derivations over a `rateLimit` record.
//
// Two components read one, and they read it for opposite purposes. The chat
// panel raises a toast when the status *transitions* somewhere alarming; the
// usage HUD renders the standing figure after every turn, alarming or not.
// The wording and the arithmetic live here once, for the same reason
// turn-cost.js and context-usage.js exist.
//
// Governing specs: specs5/5-webapp/viewers-hud.md § Usage HUD,
// specs5/5-webapp/chat.md § Engine Event Routing,
// specs5/plan/risks.md § R-6.
//
// **Why the HUD shows this at all, and why it shows it when nothing is
// wrong.** Under subscription billing the dollar figures stop mapping to
// money, and R-6's mitigation names `RateLimitEvent` as the thing that
// replaces them — "the subscription-mode equivalent of a cost signal", with
// first-class display. A section that appeared only at `allowed_warning`
// would not be a cost signal; it would be a second alarm beside the toast
// that is already the alarm. So utilisation renders from the first record of
// a session, at whatever status.
//
// The record's shape is the SDK's `RateLimitInfo`, passed through by
// `aic_dc/claude_code/messages.py::_rate_limit`:
//
//   status                   allowed | allowed_warning | rejected
//   rate_limit_type          five_hour | seven_day | seven_day_opus |
//                            seven_day_sonnet | overage
//   utilization              fraction of the window consumed, 0.0–1.0
//   resets_at                Unix *seconds* — not ISO, not milliseconds
//   overage_status           the same three words, for pay-as-you-go
//   overage_resets_at        Unix seconds
//   overage_disabled_reason  why overage is unavailable when it is

/**
 * Unix seconds → a local wall-clock time, dated when it is not today.
 *
 * A clock time rather than a countdown: nothing here ticks, and "resets in
 * 47 minutes" is wrong the moment the user looks away.
 *
 * **Why the day is part of the sentence.** The windows this labels are not
 * all short. A five-hour window resets today or, at worst, tomorrow morning,
 * and a bare "at 04:00 AM" reads correctly there. A seven-day window resets
 * up to a week out, and the same bare sentence reads as *this coming*
 * 04:00 AM — the reader plans around a wait of hours when the real wait is
 * days. So the day is appended whenever the reset falls on a local date
 * other than today's, and omitted when it does not, because "at 04:00 AM
 * today" is noise the short windows would carry on every render.
 *
 * The weekday is spelled alongside the date rather than instead of it: a
 * seven-day window can reset exactly a week out, where a weekday alone names
 * today.
 *
 * Lived in chat-panel/streaming.js until the HUD needed the same sentence.
 */
export function formatResetTime(resetsAt, now = Date.now()) {
  if (!Number.isFinite(resetsAt) || resetsAt <= 0) return '';
  const date = new Date(resetsAt * 1000);
  if (Number.isNaN(date.getTime())) return '';
  const time = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const today = new Date(now);
  const sameDay = Number.isFinite(now)
    && date.getFullYear() === today.getFullYear()
    && date.getMonth() === today.getMonth()
    && date.getDate() === today.getDate();
  if (sameDay) return `at ${time}`;
  const day = date.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' });
  return `at ${time} on ${day}`;
}

/**
 * The CLI's window name, in words.
 *
 * Unknown values pass through with their underscores opened out rather than
 * being dropped or renamed: `RateLimitType` is the CLI's enum to extend, and
 * a window this build has never heard of is still a window the user is being
 * limited by. Naming it badly beats not naming it.
 */
const TYPE_LABELS = {
  five_hour: '5-hour',
  seven_day: '7-day',
  seven_day_opus: '7-day Opus',
  seven_day_sonnet: '7-day Sonnet',
  overage: 'Overage',
};

export function limitTypeLabel(type) {
  if (typeof type !== 'string' || !type) return '';
  return TYPE_LABELS[type] || type.replace(/_/g, ' ');
}

/**
 * `utilization` as a percentage, or null when the record carries none.
 *
 * The SDK documents the field as a fraction 0.0–1.0, so this multiplies.
 * Clamped at 100 because `rejected` is reachable with a figure slightly over
 * one and a bar cannot be 103% wide; not clamped at the bottom, because a
 * negative would be a payload worth seeing rather than smoothing away — it
 * fails the finite test below instead.
 */
export function utilizationPercent(record) {
  const raw = record?.utilization;
  if (typeof raw !== 'number' || !Number.isFinite(raw) || raw < 0) return null;
  return Math.min(100, raw * 100);
}

/**
 * Whether a record still describes the window the user is in.
 *
 * A rate limit is emitted on a status *change*, so one record stands for
 * hours and outlives its own window: at `resets_at` the counter goes back to
 * zero and the utilisation figure becomes a claim about a window that no
 * longer exists. Showing "72% of your 5-hour limit" an hour after the reset
 * is worse than showing nothing, because there is no other figure on screen
 * to contradict it.
 *
 * The comparison is against the browser's clock, deliberately. It is the
 * client's question rather than the server's — a pushed record has to be aged
 * here regardless, so a second test server-side could only come to disagree
 * (specs5/next.md § C3) — and `resets_at` is already rendered against this
 * same clock by `formatResetTime`. Minutes of skew do not matter to a
 * five-hour window.
 *
 * A record with no `resets_at` is treated as open. The window is real
 * whether or not the CLI said when it ends, and the caller renders the
 * utilisation without a reset line.
 */
export function windowIsOpen(record, now = Date.now()) {
  if (!record || typeof record !== 'object') return false;
  const resetsAt = record.resets_at;
  if (!Number.isFinite(resetsAt) || resetsAt <= 0) return true;
  return resetsAt * 1000 > now;
}

/**
 * Whether a record has anything worth a section.
 *
 * A record whose window has closed has nothing to say, and neither has one
 * that carries neither a figure nor the name of a window — which is a
 * `status` and nothing else, the shape a CLI that models fewer fields than
 * this one would send.
 */
export function hasSomethingToSay(record, now = Date.now()) {
  if (!windowIsOpen(record, now)) return false;
  return utilizationPercent(record) != null
    || !!limitTypeLabel(record.rate_limit_type)
    || record.status === 'rejected';
}
