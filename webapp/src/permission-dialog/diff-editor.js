// Monaco diff editor for the `write` class.
//
// Same configuration as the main viewer (specs5/5-webapp/diff-viewer.md),
// with two differences that belong to this dialog:
//
//   - It opens scrolled to the first changed hunk. A dialog that opened
//     on line 1 of a 2 000-line file would ask the user to hunt for the
//     change they are approving
//     (permission-dialog.md § write — the diff is the feature).
//   - The right pane is read-only until the user explicitly asks to edit
//     it, and an edit swaps Allow once for Allow with edits.

import { monaco, languageForPath } from '../monaco-setup.js';
import { observeHeadStyles, syncHeadStyles } from '../shadow-style-sync.js';

/**
 * Create or re-target the diff editor for `payload`.
 *
 * Idempotent: called from `updated()` on every render, it creates the
 * editor once and afterwards only swaps models when the request changes.
 *
 * @param {object} host — the aic-permission-dialog element
 * @param {object} payload — the request being shown
 */
export function syncDiffEditor(host, payload) {
  const container = host.shadowRoot?.querySelector('.diff-host');
  if (!container) {
    // No container this render (wrong class, or a labelled case that has
    // no editor). Dispose so a stale instance cannot leak between
    // requests — the queue can move from a write to an exec request.
    disposeDiffEditor(host);
    return;
  }
  const diff = payload?.diff;
  if (!diff || diff.proposed == null) return;

  const key = `${payload.permission_id}`;
  if (host._diffEditor && host._diffKey === key) {
    applyReadOnly(host);
    return;
  }

  const language = languageForPath(diff.path || '');
  const original = monaco.editor.createModel(diff.original ?? '', language);
  const modified = monaco.editor.createModel(diff.proposed ?? '', language);

  // Monaco's stylesheets are in document.head; this editor is in a shadow
  // root, which cannot see them. Without this the dialog drew a diff at the
  // dialog's own font with Monaco's line pitch — line numbers piled onto one
  // row, every line soft-wrapped, and nothing highlighted as changed. The
  // sync runs twice on purpose: once so the container is styled before
  // layout, and once after construction because Monaco emits its theme and
  // font-metric rules synchronously *inside* the constructor, too late for
  // the first pass and too early for the observer.
  syncHeadStyles(host);

  if (!host._diffEditor) {
    try {
      host._diffEditor = monaco.editor.createDiffEditor(container, {
        theme: 'vs-dark',
        minimap: { enabled: false },
        automaticLayout: true,
        renderSideBySide: true,
        originalEditable: false,
        readOnly: true,
        scrollBeyondLastLine: false,
        renderOverviewRuler: false,
      });
    } catch (err) {
      console.error('[permission-dialog] diff editor creation failed', err);
      original.dispose();
      modified.dispose();
      return;
    }
    syncHeadStyles(host);
    if (!host._styleObserver) host._styleObserver = observeHeadStyles(host);
  }

  const previous = host._diffEditor.getModel?.();
  // The height belongs to the request, not to the editor: a 400-line diff
  // followed by a one-line one must not open at the first one's height.
  // Clearing it falls back to the stylesheet's floor, which is where the
  // measurement below grows from.
  host._diffContentHeight = null;
  container.style.removeProperty('--diff-content-height');
  try {
    host._diffEditor.setModel({ original, modified });
  } catch (err) {
    console.error('[permission-dialog] setModel failed', err);
    original.dispose();
    modified.dispose();
    return;
  }
  // Disposal order matters: setModel detaches the old pair first.
  // Disposing before setModel throws inside Monaco.
  try { previous?.original?.dispose(); } catch (_) { /* already gone */ }
  try { previous?.modified?.dispose(); } catch (_) { /* already gone */ }

  host._diffKey = key;
  attachEditListener(host);
  trackContentHeight(host);
  applyReadOnly(host);
  revealFirstChange(host);
}

/**
 * Keep the container's height on the editor's content height.
 *
 * The stylesheet clamps it between `--diff-min-height` and
 * `--diff-max-height`; all this writes is the middle term. What it buys is
 * the whole of § D2's newest case — a one-line change no longer opens 520px
 * of empty editor — and the clamp is what keeps a 3 000-line one from
 * opening a page-tall dialog instead.
 *
 * Two signals, because neither covers the other's case. A diff's alignment
 * view zones are part of the content height and do not exist until Monaco
 * has computed the diff, which is `onDidUpdateDiff`; and the right pane is
 * editable, so a user who adds ten lines has changed the content height
 * with no diff update in it, which is `onDidContentSizeChange`. Both are
 * cheap and idempotent, so subscribing to both costs a comparison.
 */
function trackContentHeight(host) {
  const editor = host._diffEditor;
  const container = host.shadowRoot?.querySelector('.diff-host');
  if (!editor || !container) return;

  const apply = () => {
    const height = Math.max(
      editor.getModifiedEditor?.()?.getContentHeight?.() ?? 0,
      editor.getOriginalEditor?.()?.getContentHeight?.() ?? 0,
    );
    // A zero means Monaco has not measured yet. Writing it would collapse
    // the pane to the floor for a frame and gain nothing.
    if (!height) return;
    // `automaticLayout` relayouts on the height this sets, which reports a
    // content height again — so a writer with no threshold is a loop.
    if (host._diffContentHeight != null
        && Math.abs(host._diffContentHeight - height) <= 2) return;
    host._diffContentHeight = height;
    container.style.setProperty('--diff-content-height', `${height}px`);
  };

  apply();
  for (const pane of [editor.getOriginalEditor?.(), editor.getModifiedEditor?.()]) {
    if (typeof pane?.onDidContentSizeChange !== 'function') continue;
    host._diffSubscriptions.push(pane.onDidContentSizeChange(apply));
  }
  if (typeof editor.onDidUpdateDiff === 'function') {
    host._diffSubscriptions.push(editor.onDidUpdateDiff(apply));
  }
}

/**
 * Scroll to the first changed hunk.
 *
 * Monaco computes the diff asynchronously, so the line numbers are not
 * available on the tick the model is set — `onDidUpdateDiff` is the only
 * reliable moment. A missing listener degrades to "opens at the top",
 * which is worse but not wrong.
 */
function revealFirstChange(host) {
  const editor = host._diffEditor;
  if (!editor || typeof editor.onDidUpdateDiff !== 'function') return;
  const subscription = editor.onDidUpdateDiff(() => {
    try { subscription.dispose(); } catch (_) { /* one-shot */ }
    const changes = editor.getLineChanges?.() || [];
    const first = changes[0];
    if (!first) return;
    const line = first.modifiedStartLineNumber
      || first.originalStartLineNumber
      || 1;
    try {
      editor.getModifiedEditor?.().revealLineInCenter(line);
    } catch (err) {
      console.warn('[permission-dialog] could not reveal the first change', err);
    }
  });
  host._diffSubscriptions.push(subscription);
}

/**
 * Watch the right pane so an edit swaps Allow once for Allow with edits.
 *
 * The swap is the point: the difference between approving what the agent
 * asked for and approving something else must be unmistakable
 * (permission-dialog.md § Editing the input).
 */
function attachEditListener(host) {
  const modified = host._diffEditor?.getModifiedEditor?.();
  if (!modified || typeof modified.onDidChangeModelContent !== 'function') return;
  const subscription = modified.onDidChangeModelContent(() => {
    if (!host._editingDiff) return;
    host._diffDirty = true;
    host.requestUpdate();
  });
  host._diffSubscriptions.push(subscription);
}

/** Push the current edit mode into the editor. */
function applyReadOnly(host) {
  const modified = host._diffEditor?.getModifiedEditor?.();
  if (!modified || typeof modified.updateOptions !== 'function') return;
  try {
    modified.updateOptions({ readOnly: !host._editingDiff });
  } catch (err) {
    console.warn('[permission-dialog] could not set read-only state', err);
  }
}

/** The proposed content as it now stands, including any user edits. */
export function currentProposedContent(host) {
  const model = host._diffEditor?.getModel?.()?.modified;
  if (!model || typeof model.getValue !== 'function') return null;
  try {
    return model.getValue();
  } catch (_) {
    return null;
  }
}

/** Tear down the editor and every listener it owns. */
export function disposeDiffEditor(host) {
  for (const subscription of host._diffSubscriptions || []) {
    try { subscription.dispose(); } catch (_) { /* already gone */ }
  }
  host._diffSubscriptions = [];
  // Before the early return below: the observer outlives an editor that was
  // never built (a request whose class has no diff host), and a dialog that
  // opens on twenty requests must not leave twenty observers on the head.
  if (host._styleObserver) {
    try { host._styleObserver.disconnect(); } catch (_) { /* already gone */ }
    host._styleObserver = null;
  }
  const editor = host._diffEditor;
  if (!editor) return;
  const models = editor.getModel?.();
  try { editor.dispose(); } catch (_) { /* already gone */ }
  try { models?.original?.dispose(); } catch (_) { /* already gone */ }
  try { models?.modified?.dispose(); } catch (_) { /* already gone */ }
  host._diffEditor = null;
  host._diffKey = null;
  host._diffContentHeight = null;
}

/** Re-apply the edit-mode flag after the user toggles it. */
export function refreshEditMode(host) {
  applyReadOnly(host);
  const modified = host._diffEditor?.getModifiedEditor?.();
  if (host._editingDiff && modified?.focus) {
    try { modified.focus(); } catch (_) { /* not focusable yet */ }
  }
}
