// Frontend parser for LLM edit blocks.
//
// Matches the backend's delimiter bytes exactly (D3 in
// IMPLEMENTATION_NOTES.md) — three orange squares, three yellow,
// three green. Do not substitute ASCII or translate.
//
// Scope: segment an assistant response into prose and edit-block
// regions so the chat panel can render text through markdown and
// edit blocks as visual cards. The segmenter is deliberately
// tolerant of incomplete blocks — mid-stream an assistant response
// may terminate at any point inside a block, and the renderer
// needs to show a "pending" card rather than treating the partial
// block as prose.
//
// Deliberate divergence from the backend parser (specs3 docs it
// explicitly):
//   - Frontend is display-only. If we miss an extensionless
//     filename like `Makefile` the block still applies correctly
//     server-side; we just render it as text.
//   - No anchor matching, no validation, no application — just
//     "where are the block boundaries".
//
// Per-file occurrence counter for matching to backend results:
// the Nth edit block for file X in a response maps to the Nth
// entry for file X in `edit_results`. Callers are responsible
// for the counting — `segmentResponse` only emits segments in
// source order with their `filePath`.

/** Start marker — literal orange-orange-orange + space + EDIT. */
export const EDIT_MARK = '🟧🟧🟧 EDIT';
/** Separator marker — yellow-yellow-yellow + space + REPL. */
export const REPL_MARK = '🟨🟨🟨 REPL';
/** End marker — green-green-green + space + END. */
export const END_MARK = '🟩🟩🟩 END';
/** Agent-spawn start marker. See specs4/3-llm/edit-protocol.md
 *  § Agent-Spawn Blocks and specs4/7-future/parallel-agents.md
 *  § Agent-spawn block format. */
export const AGENT_MARK = '🟧🟧🟧 AGENT';
/** Agent-spawn end marker — distinct from EDIT's END so a
 *  parser scanning line-by-line can dispatch on the literal
 *  line without state tracking. */
export const AGEND_MARK = '🟩🟩🟩 AGEND';

/**
 * Minimal extensionless filename whitelist.
 *
 * Backend's list is broader (`Makefile`, `Dockerfile`, `Gemfile`,
 * `Rakefile`, `Procfile`, `Brewfile`, `Justfile`, `Vagrantfile`).
 * The frontend only needs the common two for visual recognition
 * — a `Rakefile` edit still applies correctly on the backend
 * even if the frontend renders it as prose. The asymmetry is
 * deliberate (D-level decision in specs3 — not a bug).
 */
const EXTENSIONLESS_FILENAMES = new Set(['Makefile', 'Dockerfile']);

/**
 * Heuristic: is `line` plausibly a file path?
 *
 * Rules match the backend loosely (specs-reference/3-llm/edit-protocol.md#file-path-detection):
 *   - Not empty, not excessively long
 *   - Not a comment (common prefixes: #, //, *, -, >, ```)
 *   - Contains `/` or `\` — path with separators (common case)
 *   - OR matches filename-with-extension regex
 *   - OR matches dotfile regex (.gitignore, .env.local)
 *   - OR is a known extensionless name (Makefile, Dockerfile)
 *
 * Inner whitespace is not a rejection reason — filenames like
 * `docs/notes/deployment modes.md` are real, and rejecting them
 * meant the backend applied an edit the frontend rendered as
 * prose. Prose lines that now slip through (`see src/foo.py for
 * details`) are held only until the next line proves whether an
 * EDIT marker follows; `expect-edit` pushes non-blocks back to
 * the text buffer, so nothing is lost from the rendered output.
 *
 * Returns true only if exactly one of the accept rules matches.
 *
 * @param {string} line
 * @returns {boolean}
 */
export function isFilePath(line) {
  if (typeof line !== 'string') return false;
  const trimmed = line.trim();
  if (!trimmed || trimmed.length > 200) return false;

  // Comment prefixes — not paths.
  if (
    trimmed.startsWith('#') ||
    trimmed.startsWith('//') ||
    trimmed.startsWith('*') ||
    trimmed.startsWith('-') ||
    trimmed.startsWith('>') ||
    trimmed.startsWith('```')
  ) {
    return false;
  }

  // Path with separators — covers src/foo.py, a\b\c.ts, and
  // space-containing names like "docs/deployment modes.md".
  if (/[\/\\]/.test(trimmed)) return true;

  // Filename with extension (foo.js, .env.local, and
  // "deployment modes.md" — the class allows inner spaces).
  if (/^\.?[\w\-. ]+\.\w+$/.test(trimmed)) return true;

  // Dotfile without extension (.gitignore, .dockerignore).
  if (/^\.\w[\w\-.]*$/.test(trimmed)) return true;

  // Extensionless whitelist.
  if (EXTENSIONLESS_FILENAMES.has(trimmed)) return true;

  return false;
}

/**
 * @typedef {Object} TextSegment
 * @property {'text'} type
 * @property {string} content — raw text, unparsed markdown
 *
 * @typedef {Object} EditSegment
 * @property {'edit'} type
 * @property {string} filePath — path as it appeared in the response
 * @property {string} oldText — content between EDIT and REPL markers
 * @property {string} newText — content between REPL and END markers
 * @property {boolean} isCreate — true when oldText is empty/whitespace
 *
 * @typedef {Object} EditPendingSegment
 * @property {'edit-pending'} type
 * @property {string} filePath
 * @property {'expect-edit'|'reading-old'|'reading-new'} phase
 * @property {string} oldText — what has been accumulated so far
 * @property {string} newText — what has been accumulated so far
 *
 * @typedef {TextSegment | EditSegment | EditPendingSegment} Segment
 */

/**
 * Segment an assistant response into prose and edit-block regions.
 *
 * The parser is a small state machine with four states. It walks
 * lines once, accumulating text or block content according to
 * state, and emits segments in source order.
 *
 * Incomplete blocks (stream ended mid-block) produce an
 * `edit-pending` segment with whatever was accumulated up to the
 * truncation point. The frontend renders these as "pending" cards
 * with a partial diff preview.
 *
 * Code fences wrapped around edit blocks (a common LLM formatting
 * quirk) are stripped — an opening fence immediately before a file
 * path, and a closing fence immediately after `END`, both
 * disappear from the emitted text segment. Fences not adjacent to
 * blocks pass through as text.
 *
 * @param {string} text — full assistant response (may be partial
 *   during streaming)
 * @returns {Segment[]}
 */
export function segmentResponse(text) {
  if (typeof text !== 'string' || text === '') return [];

  const lines = text.split('\n');
  /** @type {Segment[]} */
  const segments = [];
  /** @type {string[]} */
  let textBuffer = [];
  let state = 'scanning';
  /** @type {string | null} */
  let pendingPath = null;
  /** @type {string | null} */
  let currentPath = null;
  /** @type {string[]} */
  let oldLines = [];
  /** @type {string[]} */
  let newLines = [];

  const flushText = () => {
    if (textBuffer.length === 0) return;
    // Strip a trailing code-fence line if present — the fence is
    // the LLM wrapping the edit block and shouldn't appear as
    // prose. Only strip one line.
    if (
      textBuffer.length > 0 &&
      /^```/.test(textBuffer[textBuffer.length - 1].trim())
    ) {
      textBuffer.pop();
    }
    const content = textBuffer.join('\n');
    if (content !== '') {
      segments.push({ type: 'text', content });
    }
    textBuffer = [];
  };

  /** @type {string[]} */
  let agentLines = [];

  // Lines consumed while in 'expect-edit' that are neither the
  // EDIT marker nor a new path candidate — blank lines and an
  // opening code fence. If the candidate turns out to be a real
  // path these are wrapper noise and correctly dropped; if it
  // turns out to be prose they must be replayed into the text
  // buffer in source order, or paragraph breaks collapse in the
  // rendered markdown. Only matters now that path-bearing prose
  // ("see src/foo.py for details") can reach 'expect-edit' —
  // before the inner-whitespace rule was lifted it could not.
  /** @type {string[]} */
  let heldLines = [];

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const stripped = line.trim();

    switch (state) {
      case 'scanning': {
        if (stripped === AGENT_MARK) {
          // Agent block starts — no preceding file path. Flush
          // any accumulated text (minus optional fence wrapper)
          // and enter the agent body.
          flushText();
          agentLines = [];
          state = 'reading-agent';
        } else if (isFilePath(stripped)) {
          // Possible start of an edit block — hold the path,
          // peek next non-blank line for EDIT marker. Hold the
          // raw line, not the trimmed one, so replaying prose
          // preserves its original indentation.
          pendingPath = line;
          heldLines = [];
          state = 'expect-edit';
        } else {
          textBuffer.push(line);
        }
        break;
      }

      case 'reading-agent': {
        if (stripped === AGEND_MARK) {
          const parsed = _parseAgentBody(agentLines);
          segments.push({
            type: 'agent',
            id: parsed.id,
            task: parsed.task,
            mode: parsed.mode,
            extras: parsed.extras,
          });
          agentLines = [];
          state = 'scanning';
          // Look ahead — strip a paired closing code fence
          // the LLM may have wrapped the block in.
          if (
            i + 1 < lines.length &&
            /^```/.test(lines[i + 1].trim())
          ) {
            i += 1;
          }
        } else {
          agentLines.push(line);
        }
        break;
      }

      case 'expect-edit': {
        if (stripped === EDIT_MARK) {
          // Confirmed block — flush any accumulated text
          // (minus optional fence wrapper) and enter the
          // old-section. The held blank/fence lines were
          // wrapper noise after all, so they are dropped.
          flushText();
          currentPath = pendingPath === null ? null : pendingPath.trim();
          pendingPath = null;
          heldLines = [];
          oldLines = [];
          newLines = [];
          state = 'reading-old';
        } else if (stripped === '') {
          // Blank line between path and marker is tolerated.
          // Held rather than dropped — see `heldLines`.
          heldLines.push(line);
        } else if (/^```/.test(stripped)) {
          // Fence line between path and EDIT marker — the
          // LLM wrapped its block in a code fence. Tolerate
          // the opening fence so the block still parses;
          // the closing fence after END is already handled
          // by the lookahead at the end of 'reading-new'.
          heldLines.push(line);
        } else if (isFilePath(stripped)) {
          // The "path" we held was actually a text line; the
          // real path candidate is this one. Push the old
          // pending path and anything held since back to the
          // text buffer, then try again.
          if (pendingPath !== null) textBuffer.push(pendingPath);
          for (const held of heldLines) textBuffer.push(held);
          heldLines = [];
          pendingPath = line;
        } else {
          // Not a block — push the pending path and held lines
          // back to text and resume scanning with this line.
          if (pendingPath !== null) {
            textBuffer.push(pendingPath);
            pendingPath = null;
          }
          for (const held of heldLines) textBuffer.push(held);
          heldLines = [];
          textBuffer.push(line);
          state = 'scanning';
        }
        break;
      }

      case 'reading-old': {
        if (stripped === REPL_MARK) {
          state = 'reading-new';
        } else {
          oldLines.push(line);
        }
        break;
      }

      case 'reading-new': {
        if (stripped === END_MARK) {
          const oldText = oldLines.join('\n');
          const newText = newLines.join('\n');
          segments.push({
            type: 'edit',
            filePath: currentPath,
            oldText,
            newText,
            isCreate: oldText.trim() === '',
          });
          currentPath = null;
          oldLines = [];
          newLines = [];
          state = 'scanning';
          // Look ahead — if the next line is a closing code
          // fence paired with the wrapper, skip it so it
          // doesn't appear as prose.
          if (
            i + 1 < lines.length &&
            /^```/.test(lines[i + 1].trim())
          ) {
            i += 1;
          }
        } else {
          newLines.push(line);
        }
        break;
      }

      default:
        // Unreachable, but defensive.
        state = 'scanning';
    }
  }

  // Handle truncation — stream ended mid-block.
  if (state === 'expect-edit' && pendingPath !== null) {
    // Saw a candidate path but no EDIT marker followed. Treat
    // as text so the user sees what the LLM typed, and replay
    // any blank/fence lines held after it.
    textBuffer.push(pendingPath);
    pendingPath = null;
    for (const held of heldLines) textBuffer.push(held);
    heldLines = [];
  }

  if (state === 'reading-old' || state === 'reading-new') {
    // Flush accumulated text before the pending block.
    flushText();
    segments.push({
      type: 'edit-pending',
      filePath: currentPath,
      phase: state,
      oldText: oldLines.join('\n'),
      newText: newLines.join('\n'),
    });
    currentPath = null;
    oldLines = [];
    newLines = [];
  }

  if (state === 'reading-agent') {
    // Stream ended mid-agent-block — emit a pending segment
    // so the renderer shows a "spawning…" card with whatever
    // body has accumulated so far.
    flushText();
    const parsed = _parseAgentBody(agentLines);
    segments.push({
      type: 'agent-pending',
      id: parsed.id,
      task: parsed.task,
      mode: parsed.mode,
      extras: parsed.extras,
    });
    agentLines = [];
  }

  flushText();
  return segments;
}

/**
 * Parse the YAML-ish body of an agent-spawn block.
 *
 * Per specs4/7-future/parallel-agents.md § Agent-spawn block
 * format, the body is a minimal `key: value` payload with three
 * known fields: `id`, `task`, `mode`. Unknown keys land in
 * `extras` for forward compatibility.
 *
 * Field-name allowlist matters here — the `task` value
 * typically spans multiple lines of markdown that may contain
 * lines like `Requirements:` or `Notes:`. Without an allowlist,
 * those would be misparsed as new fields and silently truncate
 * the task body. The allowlist gates field-start detection;
 * non-allowlisted `word: value` lines are continuation of the
 * current field.
 *
 * @param {string[]} lines — body lines between AGENT and AGEND
 * @returns {{id: string, task: string, mode: string,
 *   extras: Record<string, string>}}
 */
function _parseAgentBody(lines) {
  const allowed = new Set(['id', 'task', 'mode']);
  const fields = {};
  /** @type {string | null} */
  let currentField = null;
  /** @type {string[]} */
  let currentValue = [];
  for (const line of lines) {
    const match = /^(\w+):\s*(.*)$/.exec(line);
    if (match && allowed.has(match[1])) {
      // Flush prior field.
      if (currentField !== null) {
        fields[currentField] = currentValue.join('\n').trim();
      }
      currentField = match[1];
      currentValue = match[2] ? [match[2]] : [];
    } else if (currentField !== null) {
      // Continuation of current field's value.
      currentValue.push(line);
    }
    // Lines before the first recognised field are dropped —
    // matches the backend parser's behaviour.
  }
  if (currentField !== null) {
    fields[currentField] = currentValue.join('\n').trim();
  }
  return {
    id: typeof fields.id === 'string' ? fields.id : '',
    task: typeof fields.task === 'string' ? fields.task : '',
    mode: typeof fields.mode === 'string' ? fields.mode : '',
    extras: {},
  };
}

/**
 * Match edit segments to their corresponding backend results.
 *
 * The backend's `stream-complete.result.edit_results` is an
 * ordered array of `{file, status, message, error_type, ...}`
 * dicts. Segments from `segmentResponse` appear in source order
 * but may include multiple edits for the same file.
 *
 * Per specs3's "per-file index counter" pattern: the Nth edit
 * block for file X in the response maps to the Nth entry for
 * file X in `edit_results`. We track a cursor per file and
 * increment it as we match.
 *
 * Returns a parallel array aligned to `segments` where each
 * element is the matched result or `null` for non-edit or
 * unmatched segments.
 *
 * @param {Segment[]} segments
 * @param {Array<{file: string, status: string, message?: string,
 *   error_type?: string}>} editResults
 * @returns {Array<object|null>}
 */
export function matchSegmentsToResults(segments, editResults) {
  if (!Array.isArray(segments)) return [];
  if (!Array.isArray(editResults) || editResults.length === 0) {
    return segments.map(() => null);
  }
  // Group results by file, preserving order within each group.
  //
  // Backend key is `file_path` per
  // specs-reference/3-llm/edit-protocol.md. Fall back to `file`
  // so tests using shortened fixture shape still work.
  /** @type {Map<string, Array<object>>} */
  const byFile = new Map();
  for (const result of editResults) {
    const file =
      result &&
      (typeof result.file_path === 'string'
        ? result.file_path
        : typeof result.file === 'string'
          ? result.file
          : null);
    if (typeof file !== 'string') continue;
    if (!byFile.has(file)) byFile.set(file, []);
    byFile.get(file).push(result);
  }
  // Cursor per file tracking how many results we've consumed.
  /** @type {Map<string, number>} */
  const cursor = new Map();
  return segments.map((seg) => {
    if (seg.type !== 'edit') return null;
    const file = seg.filePath;
    if (typeof file !== 'string') return null;
    const results = byFile.get(file);
    if (!results) return null;
    const idx = cursor.get(file) || 0;
    if (idx >= results.length) return null;
    cursor.set(file, idx + 1);
    return results[idx];
  });
}