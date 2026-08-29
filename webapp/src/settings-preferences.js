// Reading and writing one field of a JSON config file, as text.
//
// What the Settings tab's preference cards are made of
// (`specs5/5-webapp/settings.md` § Preference Cards). A card holds a
// switch over a single key that is already editable in the textarea
// beside it, so what these functions have to protect is the rest of the
// file: a click on a toggle must not be how a user's config gets
// rewritten.
//
// **Reads parse; writes edit text.** They are not symmetrical because
// they answer different questions. A read wants the effective value,
// which is what `JSON.parse` computes — including the last of two
// duplicate keys, which is what the backend's own `json.loads` will
// see. A write wants to change one value and touch nothing else, and
// `JSON.stringify` cannot do that: round-tripping this app's own
// `app.json` through it explodes `extensions` and `keywords_ngram_range`
// from one line each to twelve, so the file the user reopens is not the
// file they had. That reformat is not data loss, but it is an
// unrequested change made by a control that promised to move one
// boolean.
//
// So `writePreference` replaces the value *in its line* when it safely
// can, and falls back to parse-and-stringify when it cannot — a key the
// file does not have yet, or a value that is not a scalar. The fallback
// reformats, and that is the honest trade: a file that had no such key
// was not carrying formatting for it.
//
// Governing spec: `specs5/1-foundation/configuration.md`.

/**
 * A JSON scalar, as it appears in source text.
 *
 * Only scalars are edited in place. An array or object value can span
 * lines and can contain a `,` that is not the separator, so the
 * single-line surgery below would corrupt it — those go to the
 * stringify fallback, where correctness is the parser's problem rather
 * than a regex's.
 */
const _SCALAR = String.raw`null|true|false|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|"(?:[^"\\]|\\.)*"`;

/** The line that sets `key`, as `prefix + scalar + suffix`. */
function _valueLineRe(key) {
  return new RegExp(
    `^(\\s*${_quoted(key)}\\s*:\\s*)(${_SCALAR})(\\s*,?\\s*)$`,
  );
}

/** `key` as a regex-safe JSON string literal. */
function _quoted(key) {
  return JSON.stringify(key).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** How many lines in `content` open a mapping for `key`. */
function _keyLineCount(content, key) {
  const opens = new RegExp(`^\\s*${_quoted(key)}\\s*:`);
  let count = 0;
  for (const line of content.split('\n')) {
    if (opens.test(line)) count += 1;
  }
  return count;
}

/**
 * The object `path`'s leaf lives in, or null when it is not there yet.
 *
 * A top-level path's parent is the root, which by this point is known to
 * be an object — so the answer is never "no parent" for a one-element
 * path, which is the case an `undefined`-returning read would get wrong.
 */
function _parentOf(parsed, path) {
  let node = parsed;
  for (const key of path.slice(0, -1)) {
    node = node[key];
    if (!node || typeof node !== 'object' || Array.isArray(node)) return null;
  }
  return node;
}

/**
 * The value at a dotted path, or `fallback`.
 *
 * `fallback` is returned for every way of not having an answer —
 * unparseable content, a missing intermediate, an absent key, an
 * explicit `null` — because a preference card renders one control and
 * has one thing to show. The card that needs to tell "the file says
 * null" from "the file has no such key" does not exist; if one ever
 * does, it wants its own reader rather than a second return channel
 * here.
 *
 * @param {string} content raw file text
 * @param {string[]} path top-level key first
 * @param {*} fallback
 * @returns {*}
 */
export function readPreference(content, path, fallback = null) {
  if (typeof content !== 'string' || !Array.isArray(path) || !path.length) {
    return fallback;
  }
  let node;
  try {
    node = JSON.parse(content);
  } catch {
    return fallback;
  }
  for (const key of path) {
    if (!node || typeof node !== 'object' || Array.isArray(node)) return fallback;
    node = node[key];
  }
  return node === undefined || node === null ? fallback : node;
}

/**
 * `content` with the value at `path` set to `value`, or null.
 *
 * Null means the caller must not save: the content is not a JSON object,
 * so there is no safe edit and the user's file would be replaced by
 * whatever a toggle thought it should contain. The tab says so and sends
 * the reader to the textarea, which is the surface that can fix a file
 * that does not parse.
 *
 * @param {string} content
 * @param {string[]} path
 * @param {*} value must be JSON-serialisable
 * @returns {string|null}
 */
export function writePreference(content, path, value) {
  if (typeof content !== 'string' || !Array.isArray(path) || !path.length) {
    return null;
  }
  let parsed;
  try {
    parsed = JSON.parse(content);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;

  const leaf = path[path.length - 1];
  // In place, but only when the leaf name is unambiguous in the file. A
  // second `"keywords_enabled":` under another section would make the
  // line search and `JSON.parse` disagree about which one is the value,
  // and the one that would be wrong is the write.
  if (_keyLineCount(content, leaf) === 1 && _parentOf(parsed, path) !== null) {
    const pattern = _valueLineRe(leaf);
    const lines = content.split('\n');
    for (let i = 0; i < lines.length; i += 1) {
      const match = lines[i].match(pattern);
      if (!match) continue;
      lines[i] = `${match[1]}${JSON.stringify(value)}${match[3]}`;
      return lines.join('\n');
    }
  }

  // Fallback: the key is new, nested under a section that is not there,
  // or currently holds a container. Reformats, and creates the
  // intermediates it needs.
  let node = parsed;
  for (const key of path.slice(0, -1)) {
    if (!node[key] || typeof node[key] !== 'object' || Array.isArray(node[key])) {
      node[key] = {};
    }
    node = node[key];
  }
  node[leaf] = value;
  const trailing = content.endsWith('\n') ? '\n' : '';
  return `${JSON.stringify(parsed, null, 2)}${trailing}`;
}
