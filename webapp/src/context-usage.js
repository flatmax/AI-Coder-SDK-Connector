// Shared derivations over the engine's `ContextUsageResponse`.
//
// Three components render this payload — the Context tab, the usage
// HUD, and the thin capacity bar under the dialog — and each of them
// got the same two things wrong independently, so the arithmetic lives
// here once.
//
// Governing spec: specs5/5-webapp/viewers-hud.md § Context Usage (CC-17).
//
// What a live payload actually looks like (opus-5, 200K window):
//
//   totalTokens          21087
//   maxTokens           200000
//   rawMaxTokens        200000     <- equal to maxTokens, not reduced
//   autoCompactThreshold 167000
//   categories    System prompt 3371, System tools 13315,
//                 Memory files 27, Skills 1469, Messages 2905,
//                 Autocompact buffer 33000, Free space 145913,
//                 + two `isDeferred` rows
//
// Two facts fall out of that, and both contradict what these
// components were built to assume:
//
//   1. `categories` tiles the WHOLE window, not the used part. The
//      non-deferred rows sum to exactly `maxTokens`, because the
//      engine includes "Free space" and "Autocompact buffer" as
//      categories. Segmenting a fill bar by all of them draws a bar
//      that is always 100% full, and dividing a category by
//      `totalTokens` yields shares like "Free space — 692%".
//
//   2. `maxTokens` is NOT reduced by the autocompact buffer. Every
//      one of these components carried a comment saying it was. The
//      buffer is a separate category, and the trigger point is its
//      own field:
//
//        Free space         145913 = autoCompactThreshold - totalTokens
//        Autocompact buffer  33000 = maxTokens - autoCompactThreshold
//        content categories  21087 = totalTokens
//
//      All three identities hold exactly, which is what makes the
//      partition below checkable rather than guessed.
//
//      The consequence was a dead warning: autocompact fires at 83.5%
//      of the window, so a bar measured against `maxTokens` with a
//      red band above 90% goes red only after the compaction it
//      exists to predict has already happened.

/** Fallback for a category the engine sent without a usable colour. */
export const UNCOLOURED = '#6e7681';

/**
 * Category names the engine reports that are not part of
 * `totalTokens` — they describe the room left, not what is in the
 * window.
 *
 * Matched by name because the payload carries no flag for it. That is
 * fragile, so every caller gets `verified` alongside the partition and
 * degrades to an unsegmented bar when the identity stops holding. A
 * renamed row costs us a plainer bar, not a wrong one.
 */
const _STRUCTURAL = new Set(['free space', 'autocompact buffer']);

/**
 * The engine's theme token names, mapped to CSS.
 *
 * `color` is not a colour. It carries the CLI's own theme token —
 * `promptBorder`, `inactive`, `claude`, `warning`,
 * `purple_FOR_SUBAGENTS_ONLY` — and the components pushed it straight
 * into `style="background: ${c.color}"`, which is invalid CSS. Every
 * bar segment and every swatch rendered transparent. Unit tests with
 * hex fixtures passed the whole time.
 *
 * Only tokens actually observed in a payload are mapped, plus the
 * handful whose meaning is unambiguous. Inventing CSS for tokens that
 * may not exist would just be a different kind of guess; anything
 * unrecognised falls back to grey, which reads as "uncoloured" rather
 * than as a wrong category.
 *
 * The values are this app's palette, not the terminal's — we cannot
 * read the CLI's active theme, and matching its hues exactly is not
 * worth a second source of truth. Categories stay distinguishable
 * from each other, which is what the swatch is for.
 *
 * Note `promptBorder` is used by the engine for both "System prompt"
 * and "Free space". That collision is the engine's; it is not
 * papered over here, because a swatch that disagrees with `/context`
 * would be worse than one that shares a hue.
 */
const _THEME_COLORS = {
  claude: '#d97757',
  promptBorder: '#58a6ff',
  inactive: '#6e7681',
  purple_FOR_SUBAGENTS_ONLY: '#bc8cff',
  warning: '#d29922',
  error: '#f85149',
  success: '#7ee787',
  text: '#c9d1d9',
  secondaryText: '#8b949e',
};

/**
 * Resolve a category's `color` to something CSS will accept.
 *
 * Passes real CSS colours through untouched, so a future payload that
 * sends hex or `rgb()` keeps working without a change here.
 *
 * @param {unknown} value A theme token name, or a CSS colour.
 * @returns {string} A CSS colour, never empty.
 */
export function categoryColor(value) {
  if (typeof value !== 'string') return UNCOLOURED;
  const v = value.trim();
  if (!v) return UNCOLOURED;
  if (/^#[0-9a-f]{3,8}$/i.test(v)) return v;
  if (/^(?:rgb|hsl)a?\(/i.test(v)) return v;
  return _THEME_COLORS[v] || UNCOLOURED;
}

/** Green ≤75%, amber 75-90%, red >90%. Shared by all three views. */
export function bandColor(pct) {
  if (pct > 90) return '#f85149';
  if (pct > 75) return '#d29922';
  return '#7ee787';
}

/**
 * Split `categories` into what is in the window, what is budgeted but
 * not loaded, and what is merely room.
 *
 * Zero-token rows are dropped from all three lists — they contribute
 * no width to a bar and no information to a legend.
 *
 * @param {object|null|undefined} usage A `ContextUsageResponse`.
 * @returns {{content: object[], deferred: object[], structural: object[],
 *   contentTokens: number, verified: boolean}}
 *   `verified` is true when the content rows sum to `totalTokens`,
 *   confirming the name-based split held for this payload.
 */
export function partitionCategories(usage) {
  const cats = Array.isArray(usage?.categories) ? usage.categories : [];
  const content = [];
  const deferred = [];
  const structural = [];
  for (const c of cats) {
    if (!c || typeof c !== 'object') continue;
    if (!(Number(c.tokens) > 0)) continue;
    // Deferred is checked first: a deferred row is never part of the
    // fill regardless of what it is named.
    if (c.isDeferred) deferred.push(c);
    else if (_STRUCTURAL.has(String(c.name ?? '').trim().toLowerCase())) {
      structural.push(c);
    } else content.push(c);
  }
  const contentTokens = content.reduce(
    (sum, c) => sum + (Number(c.tokens) || 0),
    0,
  );
  const total = Number(usage?.totalTokens);
  // One token of slack for rounding, 1% for a payload that counts
  // something slightly differently than it reports.
  const verified = Number.isFinite(total)
    && total > 0
    && Math.abs(contentTokens - total) <= Math.max(1, total * 0.01);
  return { content, deferred, structural, contentTokens, verified };
}

/**
 * The token count at which this session's context actually gives out.
 *
 * The autocompact threshold when autocompact is on, because that is
 * where the pause happens; the raw window when it is off, because
 * then the turn runs until the model refuses.
 *
 * @returns {number} A positive limit, or 0 when the payload has no
 *   usable window size.
 */
export function compactionLimit(usage) {
  const max = Number(usage?.maxTokens) || 0;
  if (usage?.isAutoCompactEnabled === false) return max;
  const threshold = Number(usage?.autoCompactThreshold) || 0;
  if (threshold > 0 && (max <= 0 || threshold <= max)) return threshold;
  return max;
}

/**
 * How full the context is as a fraction of where it gives out.
 *
 * This is the number the warning colours belong on. The engine's own
 * `percentage` is against the raw window and stays reassuring right
 * up to the compact.
 *
 * @returns {number|null} Percent, unclamped, or null when unknowable.
 */
export function compactionPercent(usage) {
  const limit = compactionLimit(usage);
  if (limit <= 0) return null;
  const total = Number(usage?.totalTokens);
  if (!Number.isFinite(total)) return null;
  return (total / limit) * 100;
}

/**
 * The percentage the warning colours belong on.
 *
 * The compaction-relative figure when the engine reports a threshold
 * distinct from the window, and the engine's own headline percentage
 * otherwise.
 *
 * The fallback is not just defensive. `compactionPercent` recomputes
 * from `totalTokens`, while `windowPercent` prefers the engine's
 * `percentage` field; when there is no separate threshold those two
 * denominators are the same and the engine's rounding should win, or a
 * view ends up showing one number and colouring it by another.
 *
 * @returns {number} Percent, unclamped.
 */
export function warningPercent(usage) {
  const max = Number(usage?.maxTokens) || 0;
  const limit = compactionLimit(usage);
  if (limit > 0 && max > 0 && limit < max) {
    const pct = compactionPercent(usage);
    if (pct != null) return pct;
  }
  return windowPercent(usage);
}

/**
 * The engine's headline percentage, against the raw window.
 *
 * Preferred over a local ratio so this app and `/context` cannot
 * disagree; computed only when the engine omits it.
 *
 * @returns {number} Percent, clamped to 0-100 for display.
 */
export function windowPercent(usage) {
  const total = Number(usage?.totalTokens) || 0;
  const max = Number(usage?.maxTokens) || 0;
  const pct = Number.isFinite(Number(usage?.percentage))
    ? Number(usage.percentage)
    : (max > 0 ? (total / max) * 100 : 0);
  return Math.max(0, Math.min(100, pct));
}
