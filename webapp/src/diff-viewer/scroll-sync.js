// Bidirectional editor↔preview scroll sync.
//
// Anchors are <[data-source-line]> elements emitted by
// the markdown / TeX preview pipelines. We collect them
// once per scroll event, sort + dedupe, then binary-
// search to map between editor source lines and preview
// scroll offsets.
//
// A short-lived "lock" token serialises the two sides:
// the side that initiates a scroll holds the lock, the
// other side's handler skips while it's held. Without
// this the two sides ping-pong via Monaco's smooth-
// scroll animation.

import { monaco } from '../monaco-setup.js';

import { _SCROLL_LOCK_MS } from './constants.js';

/**
 * Acquire the scroll lock for `side` ('editor' or
 * 'preview') and auto-release after a short window.
 * During the lock the other side's scroll handler
 * skips, preventing feedback loops.
 */
export function acquireScrollLock(host, side) {
  host._scrollLock = side;
  if (host._scrollLockTimer) {
    clearTimeout(host._scrollLockTimer);
  }
  host._scrollLockTimer = setTimeout(() => {
    host._scrollLock = null;
    host._scrollLockTimer = null;
  }, _SCROLL_LOCK_MS);
}

/**
 * Collect scroll anchors from the preview pane — one
 * per block element carrying data-source-line. Returns
 * a deduped, monotonically-increasing list of
 * {line, offsetTop} pairs ready for binary search.
 *
 * Dedup — first element per source line wins. Some
 * nested block elements emit the same source-line
 * attribute; keeping the first is both cheapest and
 * visually correct (outermost block).
 *
 * Monotonicity — sort by offsetTop ascending and drop
 * any anchor whose offsetTop is less than the running
 * maximum. Nested containers can have inner children
 * with earlier offsetTop than an already-seen outer
 * block; including them would make the binary search
 * jumpy.
 */
export function collectPreviewAnchors(host) {
  if (!host._previewPane) return [];
  const raw = host._previewPane.querySelectorAll(
    '[data-source-line]',
  );
  const seen = new Set();
  const entries = [];
  for (const el of raw) {
    const line = parseInt(el.dataset.sourceLine, 10);
    if (!Number.isFinite(line)) continue;
    if (seen.has(line)) continue;
    seen.add(line);
    entries.push({ line, offsetTop: el.offsetTop });
  }
  entries.sort((a, b) => a.offsetTop - b.offsetTop);
  let lastTop = -Infinity;
  return entries.filter((e) => {
    if (e.offsetTop < lastTop) return false;
    lastTop = e.offsetTop;
    return true;
  });
}

/**
 * Editor scrolled — map the top visible line to an
 * anchor in the preview pane and scroll the preview to
 * match. Skips when the lock is held by the other side.
 */
export function onEditorScroll(host) {
  if (host._scrollLock === 'preview') return;
  if (!host._previewMode || !host._previewPane) return;
  const modifiedEditor = host._getModifiedEditor();
  if (!modifiedEditor) return;
  let topLine;
  try {
    const scrollTop = modifiedEditor.getScrollTop?.() ?? 0;
    topLine = editorScrollTopToLine(host, modifiedEditor, scrollTop);
  } catch (_) {
    return;
  }
  const anchors = collectPreviewAnchors(host);
  if (anchors.length === 0) return;
  const targetTop = mapLineToOffsetTop(host, anchors, topLine);
  if (targetTop == null) return;
  acquireScrollLock(host, 'editor');
  host._previewPane.scrollTop = targetTop;
}

/**
 * Preview scrolled — find the anchor at/just before
 * the current scrollTop and scroll the editor to that
 * source line. Skips when the lock is held by the
 * editor side.
 */
export function onPreviewScroll(host) {
  if (host._scrollLock === 'editor') return;
  if (!host._previewMode || !host._previewPane) return;
  const modifiedEditor = host._getModifiedEditor();
  if (!modifiedEditor) return;
  const scrollTop = host._previewPane.scrollTop;
  const anchors = collectPreviewAnchors(host);
  if (anchors.length === 0) return;
  const targetLine = mapOffsetTopToLine(host, anchors, scrollTop);
  if (targetLine == null) return;
  acquireScrollLock(host, 'preview');
  try {
    const targetScroll = editorLineToScrollTop(
      host, modifiedEditor, targetLine,
    );
    modifiedEditor.setScrollTop?.(targetScroll);
  } catch (_) {}
}

/**
 * Pixel top of a (fractional) editor line. Uses Monaco's
 * layout so view zones (inline deleted lines, diff
 * padding) and wrapped lines are accounted for; a plain
 * `(line - 1) * lineHeight` drifts further the more of
 * those sit above the viewport.
 */
export function editorLineToScrollTop(host, editor, line) {
  if (typeof editor.getTopForLineNumber !== 'function') {
    return (line - 1) * getLineHeight(editor);
  }
  const count = getEditorLineCount(host) || Math.ceil(line);
  const whole = Math.max(1, Math.min(count, Math.floor(line)));
  const top = editor.getTopForLineNumber(whole);
  const next = whole < count
    ? editor.getTopForLineNumber(whole + 1)
    : top + getLineHeight(editor);
  return top + (line - whole) * (next - top);
}

/**
 * Inverse of editorLineToScrollTop — binary-search the
 * line whose top is at/just above `scrollTop`, then add
 * the fraction of the way to the next line.
 */
export function editorScrollTopToLine(host, editor, scrollTop) {
  if (typeof editor.getTopForLineNumber !== 'function') {
    return scrollTop / getLineHeight(editor) + 1;
  }
  const count = getEditorLineCount(host);
  if (!count) return 1;
  let lo = 1;
  let hi = count;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (editor.getTopForLineNumber(mid) <= scrollTop) lo = mid;
    else hi = mid - 1;
  }
  const top = editor.getTopForLineNumber(lo);
  const next = lo < count
    ? editor.getTopForLineNumber(lo + 1)
    : top + getLineHeight(editor);
  if (next <= top) return lo;
  return lo + Math.min(1, (scrollTop - top) / (next - top));
}

/**
 * Binary-search anchors by source line. Returns the
 * interpolated offsetTop between the matched anchor
 * and the next one. Past the last anchor, falls back
 * to proportional mapping so reaching the editor
 * bottom scrolls the preview to its bottom too.
 */
export function mapLineToOffsetTop(host, anchors, line) {
  if (anchors.length === 0) return null;
  if (line <= anchors[0].line) return anchors[0].offsetTop;
  const last = anchors[anchors.length - 1];
  if (line >= last.line) {
    if (!host._previewPane) return last.offsetTop;
    const total = host._previewPane.scrollHeight -
      host._previewPane.clientHeight;
    const maxEditorLines = getEditorLineCount(host);
    if (!maxEditorLines || line >= maxEditorLines) {
      return total;
    }
    const frac = (line - last.line) /
      (maxEditorLines - last.line);
    return last.offsetTop +
      frac * (total - last.offsetTop);
  }
  let lo = 0;
  let hi = anchors.length - 1;
  while (lo < hi - 1) {
    const mid = (lo + hi) >> 1;
    if (anchors[mid].line <= line) lo = mid;
    else hi = mid;
  }
  const a = anchors[lo];
  const b = anchors[hi];
  if (a.line === b.line) return a.offsetTop;
  const t = (line - a.line) / (b.line - a.line);
  return a.offsetTop + t * (b.offsetTop - a.offsetTop);
}

/**
 * Inverse of mapLineToOffsetTop — find the source line
 * corresponding to a preview scroll position.
 */
export function mapOffsetTopToLine(host, anchors, offsetTop) {
  if (anchors.length === 0) return null;
  if (offsetTop <= anchors[0].offsetTop) return anchors[0].line;
  const last = anchors[anchors.length - 1];
  if (offsetTop >= last.offsetTop) {
    if (!host._previewPane) return last.line;
    const total = host._previewPane.scrollHeight -
      host._previewPane.clientHeight;
    const maxEditorLines = getEditorLineCount(host);
    if (!maxEditorLines || total <= last.offsetTop) {
      return last.line;
    }
    const frac = (offsetTop - last.offsetTop) /
      (total - last.offsetTop);
    return last.line + frac * (maxEditorLines - last.line);
  }
  let lo = 0;
  let hi = anchors.length - 1;
  while (lo < hi - 1) {
    const mid = (lo + hi) >> 1;
    if (anchors[mid].offsetTop <= offsetTop) lo = mid;
    else hi = mid;
  }
  const a = anchors[lo];
  const b = anchors[hi];
  if (a.offsetTop === b.offsetTop) return a.line;
  const t = (offsetTop - a.offsetTop) /
    (b.offsetTop - a.offsetTop);
  return a.line + t * (b.line - a.line);
}

export function getLineHeight(modifiedEditor) {
  try {
    const opts = modifiedEditor.getOption?.(
      monaco.editor.EditorOption?.lineHeight,
    );
    if (typeof opts === 'number' && opts > 0) return opts;
  } catch (_) {}
  // Reasonable default — matches Monaco's dark theme.
  return 19;
}

export function getEditorLineCount(host) {
  const modifiedEditor = host._getModifiedEditor();
  if (!modifiedEditor) return 0;
  try {
    const model = modifiedEditor.getModel?.();
    return model?.getLineCount?.() || 0;
  } catch (_) {
    return 0;
  }
}