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
//
// Phase 6 stopped guessing the rest of the payload. The `claude` binary
// carries the wire schema it validates this response against, as a zod
// object, and it reads:
//
//   categories           [{name, tokens, color, isDeferred?}]
//   memoryFiles          [{path, type, tokens}]
//   mcpTools             [{name, serverName, tokens, isLoaded?}]
//   deferredBuiltinTools [{name, tokens, isLoaded}]?
//   systemTools          [{name, tokens}]?
//   systemPromptSections [{name, tokens}]?
//   agents               [{agentType, source, tokens}]
//   slashCommands        {totalCommands, includedCommands, tokens}?
//   skills               {totalSkills, includedSkills, tokens,
//                         skillFrontmatter: [{name, source, tokens}]}?
//   messageBreakdown     {toolCallTokens, toolResultTokens,
//                         attachmentTokens, assistantMessageTokens,
//                         userMessageTokens, redirectedContextTokens,
//                         unattributedTokens,
//                         toolCallsByType: [{name, callTokens,
//                                            resultTokens}],
//                         attachmentsByType: [{name, tokens}]}?
//   apiUsage             {input_tokens, output_tokens,
//                         cache_creation_input_tokens,
//                         cache_read_input_tokens} | null
//
// That is a schema rather than a sample, so it covers the fields a live
// capture happened not to exercise — `agents[]`, which came back empty
// and was the last shape in the payload resting on a guess, and the
// theme tokens for categories a session with loaded MCP tools or custom
// agents reports. Both are below.
//
// The same read settles two things about the category list that a
// single capture could not:
//
//   - **The reserve row is named for the mode it is in.** With
//     autocompact on it is "Autocompact buffer"; with autocompact off
//     the engine still holds tokens back and calls the row "Compact
//     buffer". Both are room rather than content, so both are
//     structural. Under a window sized `auto` there is no reserve row
//     at all.
//   - **`percentage` is `round(totalTokens / rawMaxTokens * 100)`**, and
//     `maxTokens` and `rawMaxTokens` are assigned from one variable, so
//     they are equal by construction and not merely equal in the
//     capture.

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
 *
 * "Compact buffer" is the same reserve under a different name: the
 * engine picks that name when autocompact is *off*, and it was missing
 * here, so every autocompact-off session lost its segmented bar to the
 * degrade path. Read from the CLI's own category builder rather than
 * from a payload — a capture of a session with autocompact enabled
 * cannot show it.
 */
const _STRUCTURAL = new Set([
  'free space',
  'autocompact buffer',
  'compact buffer',
]);

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
 * `cyan_FOR_SUBAGENTS_ONLY` and `permission` are the exception, and the
 * reason they are safe is that they were not guessed either: the CLI's
 * category builder assigns them to "MCP tools" and "Custom agents", two
 * rows the live capture could not show because the session that
 * produced it had every MCP tool deferred and no custom agents. Grey is
 * the right answer for an unknown token and the wrong one for the row
 * naming our own bridge.
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
  cyan_FOR_SUBAGENTS_ONLY: '#39c5cf',
  permission: '#a5d6ff',
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

/**
 * Green ≤75%, amber 75-90%, red >90%. Shared by all three views.
 *
 * The engine bands the same pressure by *distance* rather than by
 * proportion — its own reader warns within 20 000 tokens of the
 * effective ceiling and calls the last 3 000 before the raw window
 * blocked. That is the better rule for a 1 M-token window, where 20 000
 * tokens is 2% and this function is still green. It is kept as a
 * proportion anyway, because these bands also colour a figure the
 * engine has no equivalent for — see `warningPercent` — and one band
 * rule that all three views share beats two that disagree. Worth
 * revisiting the day a 1 M window is the common case.
 */
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
 * Where to draw the autocompact mark on a bar measured against
 * `maxTokens`.
 *
 * The mark is the point of the gauge: "68% full" does not answer "am I
 * about to be compacted?", and the answer is 16 percentage points below
 * the end of the bar. Null means there is nothing honest to mark —
 * autocompact is off, the engine reported no threshold, or the
 * threshold is the window itself, in which case the mark would sit on
 * the bar's own end and read as a limit inside the limit.
 *
 * @returns {number|null} Percent of `maxTokens`, or null.
 */
export function thresholdPercent(usage) {
  if (usage?.isAutoCompactEnabled === false) return null;
  const max = Number(usage?.maxTokens) || 0;
  const threshold = Number(usage?.autoCompactThreshold) || 0;
  if (max <= 0 || threshold <= 0 || threshold >= max) return null;
  return (threshold / max) * 100;
}

/**
 * The context past the model's window, when it is.
 *
 * A reachable state rather than a defensive one: the engine reports it
 * with a message of its own, and a gauge that clamps at 100% and says
 * nothing is the same failure as the green 84% at the moment of a
 * compact. `kind` follows the engine's own split — a window configured
 * `auto` makes this a hard limit, anything else makes it a compaction
 * window that has been overshot.
 *
 * Measured against `rawMaxTokens`, as the engine measures it, falling
 * back to `maxTokens` for a payload that omits it. The two are equal by
 * construction today.
 *
 * @returns {{over: number, window: number,
 *   kind: 'hard_limit'|'compaction_window'}|null}
 */
export function overLimit(usage) {
  const window = Number(usage?.rawMaxTokens) || Number(usage?.maxTokens) || 0;
  const total = Number(usage?.totalTokens);
  if (window <= 0 || !Number.isFinite(total) || total <= window) return null;
  return {
    over: total - window,
    window,
    kind: usage?.autocompactSource === 'auto'
      ? 'hard_limit'
      : 'compaction_window',
  };
}

/**
 * The message parts the engine accounts for, in conversation order.
 *
 * Order is fixed rather than ranked. These are segments of one bar and
 * rows of one table across repeated fetches, and a bar whose colours
 * reorder every refresh cannot be read at a glance — the ranking is
 * what the bar's own widths show.
 *
 * The colours are ours, not the engine's, and that is the one place in
 * this module where a local palette is correct: `categories` carries
 * `color` and these parts do not, so there is nothing to adopt. They
 * are deliberately outside the hues `_THEME_COLORS` maps, so a message
 * part is never mistaken for a category.
 */
const _MESSAGE_PARTS = [
  { key: 'userMessageTokens', label: 'User messages', color: '#7ee787' },
  {
    key: 'assistantMessageTokens',
    label: 'Assistant messages',
    color: '#79c0ff',
  },
  { key: 'toolCallTokens', label: 'Tool calls', color: '#ffa657' },
  { key: 'toolResultTokens', label: 'Tool results', color: '#d29922' },
  { key: 'attachmentTokens', label: 'Attachments', color: '#f778ba' },
  {
    key: 'redirectedContextTokens',
    label: 'Redirected context',
    color: '#8b949e',
  },
  { key: 'unattributedTokens', label: 'Unattributed', color: '#484f58' },
];

/** Sum a payload field, treating anything unusable as zero. */
function _tokens(value) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

/**
 * What the conversation itself is made of, from `messageBreakdown`.
 *
 * The parts are shares of the "Messages" category, and the engine
 * derives the last of them — `unattributedTokens` — as that category
 * minus the other six, floored at zero. So the parts sum to the
 * category exactly, *except* when the engine's own message estimate
 * exceeds what the API charged for the same history: then the floor
 * bites, the parts overshoot, and the difference is real rather than
 * arithmetic. `reconciled` is that comparison, and a caller renders the
 * bar against `partsTokens` either way so the segments always tile the
 * bar they are in.
 *
 * @returns {{parts: object[], partsTokens: number,
 *   messagesTokens: number|null, byTool: object[],
 *   byAttachment: object[], reconciled: boolean}|null}
 *   Null when the engine sent no breakdown, or sent one with nothing in
 *   it — a session with no turns yet.
 */
export function messageComposition(usage) {
  const mb = usage?.messageBreakdown;
  if (!mb || typeof mb !== 'object' || Array.isArray(mb)) return null;

  const parts = _MESSAGE_PARTS
    .map(({ key, label, color }) => ({
      key,
      label,
      color,
      tokens: _tokens(mb[key]),
    }))
    .filter((p) => p.tokens > 0);
  const partsTokens = parts.reduce((sum, p) => sum + p.tokens, 0);

  const byTool = (Array.isArray(mb.toolCallsByType) ? mb.toolCallsByType : [])
    .filter((t) => t && typeof t === 'object')
    .map((t) => ({
      name: String(t.name ?? 'unknown'),
      callTokens: _tokens(t.callTokens),
      resultTokens: _tokens(t.resultTokens),
      tokens: _tokens(t.callTokens) + _tokens(t.resultTokens),
    }))
    .filter((t) => t.tokens > 0)
    // The engine sorts these; sorting again costs nothing and means a
    // payload that stops sorting does not silently scramble the table.
    .sort((a, b) => b.tokens - a.tokens);

  const byAttachment = (
    Array.isArray(mb.attachmentsByType) ? mb.attachmentsByType : []
  )
    .filter((a) => a && typeof a === 'object')
    .map((a) => ({
      name: String(a.name ?? 'unknown'),
      tokens: _tokens(a.tokens),
    }))
    .filter((a) => a.tokens > 0)
    .sort((a, b) => b.tokens - a.tokens);

  if (parts.length === 0 && byTool.length === 0 && byAttachment.length === 0) {
    return null;
  }

  const messages = (Array.isArray(usage?.categories) ? usage.categories : [])
    .find((c) => String(c?.name ?? '').trim().toLowerCase() === 'messages');
  const messagesTokens = messages ? _tokens(messages.tokens) : null;
  const reconciled = messagesTokens != null
    && messagesTokens > 0
    && Math.abs(partsTokens - messagesTokens)
      <= Math.max(1, messagesTokens * 0.01);

  return {
    parts,
    partsTokens,
    messagesTokens,
    byTool,
    byAttachment,
    reconciled,
  };
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
