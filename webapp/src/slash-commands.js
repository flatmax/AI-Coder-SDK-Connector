// Slash-command detection and filtering for the chat composer.
//
// Three pure functions and no DOM: the palette renders what
// these decide, and the chat panel's textarea handler asks
// them what the cursor is sitting in. Split out for the same
// reason file-mentions.js is — the interesting part is the
// boundary rules, and they are worth testing without mounting
// a chat panel.
//
// Why the trigger is "first non-whitespace character of the
// composer" rather than "@-style, anywhere": that is the
// engine's own rule. `ClaudeCodeService._slash_response`
// treats a message as a command only when `message.strip()`
// starts with `/`, so a slash anywhere else is prose — a
// path, a date, a fraction, a closing tag — and opening a
// palette over it would be wrong as well as annoying.

/**
 * Find the command token the cursor is inside, if any.
 *
 * Returns `{start, end, query}` or null. `start` is the index
 * of the `/`; `end` is the index one past the token, which is
 * the next whitespace or the end of the string; `query` is
 * everything between the slash and the *cursor*.
 *
 * The asymmetry is deliberate. `query` stops at the cursor so
 * filtering narrows as you type, exactly like the mention
 * filter. `end` runs to the whole token so a selection can
 * replace all of it — without that, completing `/con|text`
 * from mid-token would leave `/contexttext` behind.
 *
 * Returns null when:
 *   - the composer is empty or all whitespace
 *   - the first non-whitespace character isn't `/`
 *   - the cursor is at or before the `/`
 *   - the cursor has moved past the token's whitespace
 *     terminator (`/context |` — the command is settled and
 *     the user is typing arguments)
 *
 * @param {string} value — full textarea value
 * @param {number} cursor — `selectionStart`
 * @returns {{start: number, end: number, query: string} | null}
 */
export function detectActiveSlash(value, cursor) {
  const text = typeof value === 'string' ? value : '';
  const at = typeof cursor === 'number' ? cursor : 0;
  // Leading whitespace is tolerated — the engine strips it
  // before testing for the slash, so the palette must agree
  // or the two would disagree about what got sent.
  const start = text.search(/\S/);
  if (start === -1 || text[start] !== '/') return null;
  const terminator = text.slice(start).search(/\s/);
  const end = terminator === -1 ? text.length : start + terminator;
  if (at <= start || at > end) return null;
  return { start, end, query: text.slice(start + 1, at) };
}

/**
 * Rank one command against a lowercased needle. Lower is
 * better; null means no match.
 *
 * Descriptions are deliberately NOT searched. A skill's
 * description runs to a paragraph of trigger keywords, so
 * matching them surfaces entries whose reason for appearing
 * is invisible in the row the user is looking at — the list
 * stops feeling like it is responding to what they typed.
 */
function _rank(command, needle) {
  const name = String(command?.name || '').toLowerCase();
  if (!name) return null;
  if (name === needle) return 0;
  if (name.startsWith(needle)) return 1;
  const aliases = Array.isArray(command?.aliases) ? command.aliases : [];
  if (aliases.some((alias) => String(alias).toLowerCase().startsWith(needle))) {
    return 2;
  }
  // Substring last, so `review` still finds `code-review`
  // without outranking a genuine prefix hit.
  if (name.includes(needle)) return 3;
  return null;
}

/**
 * Filter and rank a command list against the typed query.
 *
 * An empty query returns everything, in the order the service
 * supplied (alphabetical). Ties inside a rank keep that same
 * order, so the list never reshuffles for reasons the user
 * can't see.
 *
 * @param {Array<object>} commands — from `list_commands`
 * @param {string} query — the token after `/`, unmodified
 * @returns {Array<object>}
 */
export function filterCommands(commands, query) {
  const list = Array.isArray(commands) ? commands : [];
  const needle = String(query || '').toLowerCase();
  if (!needle) return list.slice();
  const scored = [];
  list.forEach((command, index) => {
    const rank = _rank(command, needle);
    if (rank !== null) scored.push({ command, rank, index });
  });
  scored.sort((a, b) => a.rank - b.rank || a.index - b.index);
  return scored.map((entry) => entry.command);
}

/**
 * The text a selected command should leave in the composer.
 *
 * A trailing space only when the command takes arguments —
 * that's the cursor landing where the next thing goes. For an
 * argument-less command the space would be noise the user has
 * to delete or send.
 *
 * @param {object} command
 * @returns {string}
 */
export function completionFor(command) {
  const name = String(command?.name || '');
  if (!name) return '';
  return command?.argument_hint ? `/${name} ` : `/${name}`;
}
