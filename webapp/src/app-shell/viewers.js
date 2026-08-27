// Viewer routing helpers for the app shell.
//
// Extracted from app-shell.js. These functions take a `host`
// parameter (the AppShell LitElement instance). The host
// retains ownership of methods these helpers call back into
// (`_saveViewportState`, `_saveLastOpenFile`, `_getFileNav`).
//
// Governing specs:
//   - specs4/5-webapp/diff-viewer.md (event routing, refresh)
//   - specs4/5-webapp/svg-viewer.md  (SVG ↔ text-diff swap)
//   - specs4/5-webapp/file-navigation.md (navigate-file flow)

import { toRepoPath } from '../repo-path.js';
import { viewerForPath } from '../viewer-routing.js';
import { rememberDiffViewport } from './viewport.js';

/**
 * Route a `navigate-file` event to the appropriate viewer
 * based on the file's extension. Dispatches `openFile` on
 * the target viewer. The viewer's `active-file-changed`
 * event then triggers visibility toggling.
 *
 * The `_remote` flag on broadcasts is consumed by the
 * chat panel / picker to suppress re-broadcasts — we
 * don't care about it here. Same routing applies
 * whether the event came from a local click or a
 * collaboration broadcast.
 */
export function onNavigateFile(host, event) {
  const detail = event.detail || {};
  // Absolute in, repo-relative out. Tool cards and the context tab carry the
  // paths the engine reports, which are absolute because Claude Code's file
  // tools require that; the viewers ask `Repo.get_file_content`, which takes
  // repo-relative paths and refuses absolute ones. Normalising here rather
  // than at each dispatcher covers every one of them at once, and covers the
  // two side effects below as well — an absolute path used to be what got
  // persisted as the last-open file and registered with the nav grid, so the
  // failure survived a reload.
  const path = toRepoPath(detail.path, host._repoRoot);
  if (typeof path !== 'string' || !path) return;
  let target = viewerForPath(path);
  if (!target) return;
  // Edit-block clicks on SVG files carry a scroll hint
  // (`searchText` from the edit anchor, or a `line`).
  // The visual SVG viewer can't honor those — its
  // openFile signature has no notion of "scroll to this
  // text" — so the anchor would be dropped and the user
  // would land on the canvas with no indication of where
  // the edit happened. Route to the text diff viewer
  // instead when a scroll hint is present, matching what
  // happens on every other file type. The user can
  // toggle back to the visual editor from the diff
  // viewer's mode switch if they want.
  const hasScrollHint =
    (typeof detail.searchText === 'string' && detail.searchText)
    || typeof detail.line === 'number';
  if (target === 'svg' && hasScrollHint) {
    target = 'diff';
  }
  // Save viewport of the current file before navigating
  // away (so switching files preserves the prior file's
  // scroll state in localStorage).
  try {
    host._saveViewportState();
  } catch (_) {
    // Don't let a save failure block navigation.
  }
  // Record the outgoing file's viewport in the in-session
  // per-path memory too, so Alt+Arrow back to it restores
  // scroll / cursor / preview pane. localStorage holds a
  // single slot for the last-opened file only; the map
  // covers "return to any file visited this session".
  //
  // MUST happen synchronously here, before the deferred
  // openFile below swaps the Monaco model — a capture
  // after the swap reads the incoming file's zero scroll.
  //
  // Skipped on `_refresh` (the reload-restore dispatch in
  // doReopenLastFile). At that point the diff viewer is
  // either empty or still showing a pre-restore state, so
  // there's nothing worth remembering, and capturing would
  // race the localStorage restore for the same path.
  if (!detail._refresh) {
    try {
      rememberDiffViewport(host);
    } catch (_) {
      // Same policy as the save above — a broken capture
      // must not block the navigation.
    }
  }
  // Persist the new path so page refresh reopens it.
  host._saveLastOpenFile(path);
  // Register with the file navigation grid unless the
  // event came from the grid itself or is a programmatic
  // refresh.
  if (!detail._fromNav && !detail._refresh) {
    const nav = host._getFileNav();
    if (nav) nav.openFile(path);
  }
  // Flip the active viewer to match the resolved target
  // so the text diff is actually visible when we routed
  // an SVG path there. Without this, the SVG viewer
  // stays foregrounded and the diff viewer's openFile
  // happens behind it. Only set when the routing
  // diverges from the path's natural viewer — for
  // non-SVG files, `active-file-changed` from the diff
  // viewer takes care of foregrounding.
  if (target === 'diff' && viewerForPath(path) === 'svg') {
    host._activeViewer = 'diff';
  }
  // Defer until the viewers exist in the DOM. Normally
  // they're rendered from the first template commit and
  // this is synchronous; the guard protects against
  // navigate-file firing before first render (rare,
  // but possible during startup).
  host.updateComplete.then(() => {
    const viewer =
      target === 'svg'
        ? host.shadowRoot?.querySelector('aic-svg-viewer')
        : host.shadowRoot?.querySelector('aic-diff-viewer');
    if (!viewer) return;
    viewer.openFile({
      path,
      line: detail.line,
      searchText: detail.searchText,
    });
  });
}

/**
 * Route a `load-diff-panel` event to the diff viewer's
 * loadPanel method. Dispatched by the history browser's
 * context menu for ad-hoc comparison. Shows the diff
 * viewer (switches active viewer if currently on SVG)
 * so the user sees the result immediately.
 */
export function onLoadDiffPanel(host, event) {
  const detail = event.detail || {};
  const { content, panel, label } = detail;
  if (typeof content !== 'string') return;
  if (panel !== 'left' && panel !== 'right') return;
  host._activeViewer = 'diff';
  host.updateComplete.then(() => {
    const viewer =
      host.shadowRoot?.querySelector('aic-diff-viewer');
    if (!viewer || typeof viewer.loadPanel !== 'function') {
      return;
    }
    viewer.loadPanel(content, panel, label);
  });
}

/**
 * Route a `load-svg-panel` event to the SVG viewer's
 * loadPanel method. Dispatched by the file picker's
 * "Open in left/right panel" actions when the file is
 * an SVG — the user wants a rendered visual comparison
 * rather than the XML source. Shows the SVG viewer so
 * the result is immediately visible.
 *
 * Two successive calls (one per panel) populate the
 * left and right panes of the SVG viewer's existing
 * two-pane layout, replacing whatever HEAD/working
 * comparison was previously shown.
 */
export function onLoadSvgPanel(host, event) {
  const detail = event.detail || {};
  const { content, panel, label, path } = detail;
  if (typeof content !== 'string') return;
  if (panel !== 'left' && panel !== 'right') return;
  host._activeViewer = 'svg';
  host.updateComplete.then(() => {
    const viewer =
      host.shadowRoot?.querySelector('aic-svg-viewer');
    if (!viewer || typeof viewer.loadPanel !== 'function') {
      return;
    }
    // Path is optional — historical callers (or the
    // history browser, which loads from session
    // archives without an on-disk source) may pass
    // null. The viewer treats absence as "no save
    // target" and falls back to the in-memory
    // snapshot semantics.
    viewer.loadPanel(content, panel, label, path || null);
  });
}

/**
 * Handle `toggle-svg-mode` from either viewer. Switches
 * between the visual SVG viewer and the Monaco text diff
 * editor for the same file, carrying content and dirty
 * state across.
 */
export function onToggleSvgMode(host, event) {
  const detail = event.detail || {};
  const { path, target, modified, savedContent } = detail;
  if (!path || !target) return;
  // After the swap completes (one frame lets the
  // target viewer's openFile chain settle), save
  // viewport state so the new `type` — reflecting
  // which viewer is now active — is persisted. Per
  // specs-reference/5-webapp/shell.md save-triggers
  // table, `toggle-svg-mode` is a standalone save
  // point so a reload right after the toggle lands
  // back on the intended viewer rather than the
  // pre-toggle one.
  const saveAfterSwap = () => {
    requestAnimationFrame(() => {
      try {
        host._saveViewportState();
      } catch (err) {
        console.debug(
          '[app-shell] viewport save on svg-mode toggle failed',
          err,
        );
      }
    });
  };
  host.updateComplete.then(() => {
    const diffViewer =
      host.shadowRoot?.querySelector('aic-diff-viewer');
    const svgViewer =
      host.shadowRoot?.querySelector('aic-svg-viewer');
    if (target === 'diff') {
      // Visual → text diff.
      host._activeViewer = 'diff';
      if (diffViewer) {
        // No explicit closeFile — diffViewer.openFile
        // replaces the active file (single-file model,
        // D18). A closeFile would produce a one-frame
        // empty-state flash between the two calls.
        diffViewer.openFile({ path }).then(() => {
          // If we have modified content from the SVG
          // editor, update the diff viewer's active
          // file so visual edits appear as dirty in
          // text mode. _file is the single slot in the
          // no-cache model; _files[] is gone.
          if (typeof modified === 'string' && diffViewer._file?.path === path) {
            diffViewer._file = {
              ...diffViewer._file,
              modified,
              savedContent:
                typeof savedContent === 'string'
                  ? savedContent
                  : diffViewer._file.savedContent,
            };
            diffViewer._recomputeDirty?.();
            diffViewer._showEditor?.();
          }
          saveAfterSwap();
        });
      }
    } else if (target === 'visual') {
      // Text diff → visual.
      host._activeViewer = 'svg';
      if (svgViewer && diffViewer) {
        // Read latest content from the diff viewer's
        // single active-file slot (D18 — _files[] is
        // gone).
        const diffFile =
          diffViewer._file?.path === path
            ? diffViewer._file
            : null;
        const latestModified = diffFile?.modified;
        const latestSaved = diffFile?.savedContent;
        // Diff viewer's closeFile is still called because
        // the user is leaving the text diff entirely;
        // that returns the viewer to its empty state.
        // The SVG viewer still uses the multi-file model,
        // so closeFile+openFile there is the normal swap.
        diffViewer.closeFile(path);
        svgViewer.closeFile(path);
        svgViewer.openFile({
          path,
          ...(typeof latestModified === 'string'
            ? { modified: latestModified }
            : {}),
        }).then(() => {
          if (typeof latestSaved === 'string') {
            const svgFile = svgViewer._files?.find(
              (f) => f.path === path,
            );
            if (svgFile) {
              svgFile.savedContent = latestSaved;
              svgViewer._recomputeDirtyCount();
            }
          }
          saveAfterSwap();
        });
      }
    }
  });
}

/**
 * Handle `active-file-changed` bubbling up from either
 * viewer. When a viewer reports it has an active file,
 * that viewer becomes visible. When it reports null
 * (no files open), we keep the currently-visible viewer
 * as-is — flipping to the other one would just show
 * its empty state, which isn't what the user wants.
 *
 * Uses `event.composedPath()` to identify which viewer
 * emitted the event, so the handler is robust even if
 * additional viewers are added later.
 */
export function onActiveFileChanged(host, event) {
  const detail = event.detail || {};
  if (!detail.path) return;
  // Identify the source viewer by walking the composed
  // path — the event originates inside the viewer's
  // shadow root and bubbles up through the host element.
  const path = event.composedPath ? event.composedPath() : [];
  let newActive = null;
  for (const el of path) {
    if (el && el.tagName === 'AIC-SVG-VIEWER') {
      newActive = 'svg';
      break;
    }
    if (el && el.tagName === 'AIC-DIFF-VIEWER') {
      newActive = 'diff';
      break;
    }
  }
  if (!newActive) return;
  host._activeViewer = newActive;
  // Save a baseline viewport now that the viewer has a
  // file active. Without this, a user who opens an SVG
  // and reloads without any further interaction has no
  // persisted record of which viewer was active — the
  // stored `aic-last-viewport` still reflects the
  // previously-open file, and the restore short-circuits
  // because `viewport.path !== fileToRestore`. Saving
  // here captures the `type` discriminator (svg vs diff)
  // the moment the viewer reports its file, so every
  // reload has the right routing info regardless of
  // whether the user interacted with the viewer.
  //
  // For the SVG viewer this also covers the case where
  // the editors haven't finished attaching yet —
  // `_saveSvgViewportState` will see `getActiveViewBox()`
  // return null and the save becomes a no-op for that
  // trigger, but subsequent `viewbox-changed` events
  // (initial fit, first pan/zoom) arrive with editors
  // live and populate the viewBox block then.
  //
  // Wrapped in try/catch so a broken save never trips
  // downstream viewer-swap logic — the viewport save
  // is a best-effort persistence, not a correctness
  // requirement for the current render.
  try {
    host._saveViewportState();
  } catch (err) {
    console.debug(
      '[app-shell] viewport save on active-file-changed failed',
      err,
    );
  }
}

/**
 * RAF-throttled viewer relayout. Multiple calls within
 * a single animation frame coalesce to one actual
 * relayout call on each viewer. Called from both the
 * window-resize path and the dialog-resize pointermove
 * path; distinct from `_resizeRAF` so the two paths
 * don't cancel each other's pending frames.
 */
export function scheduleViewerRelayout(host) {
  if (host._viewerRelayoutRAF) return;
  host._viewerRelayoutRAF = requestAnimationFrame(() => {
    host._viewerRelayoutRAF = null;
    relayoutViewers(host);
  });
}

/**
 * Keep the viewer background clear of the docked dialog by
 * publishing `--viewer-inset-left` on the background layer.
 *
 * The background spans the full viewport while the dialog
 * is an opaque panel docked to the left edge at 50% width.
 * Both viewers split their width 50/50, so the "original"
 * side of every side-by-side view used to sit exactly
 * underneath the dialog — the SVG viewer's left pane was
 * present, visible per CSS, correctly holding HEAD content,
 * and impossible to see. Presentation mode had the mirror
 * problem: its full-width right pane spilled its left half
 * under the dialog too.
 *
 * The inset is measured from the dialog's own rect rather
 * than recomputed from `_dockedWidth` or the stylesheet's
 * `width: 50%`. The measured right edge already accounts
 * for `min-width`, the 1px border, and an in-flight resize
 * drag that has written `dialog.style.width` without yet
 * committing it to reactive state — three ways a
 * recomputation would drift.
 *
 * Zero whenever the dialog isn't occluding a full-height
 * strip: undocked (it floats over the layer, so there's no
 * strip to reserve — moving the viewer for it would make
 * the content jump on every drag) or minimized (collapsed
 * to its tab strip at the top). Minimizing therefore hands
 * the whole viewport to the viewer, which is what a user
 * reaching for presentation mode wants.
 *
 * @returns {boolean} whether the value changed. Callers use
 *   this to relayout only when the available width actually
 *   moved — the viewers' `relayout()` refits viewBoxes and
 *   Monaco layout, which is wasted work otherwise.
 */
export function syncViewerInset(host) {
  const bg = host.shadowRoot?.querySelector('.viewer-background');
  if (!bg) return false;
  const dialog = host.shadowRoot?.querySelector('.dialog');
  let value = '0px';
  if (
    dialog
    && !host._minimized
    && !dialog.classList.contains('floating')
  ) {
    const rect = dialog.getBoundingClientRect();
    // Docked means flush against the left edge. The `left`
    // guard keeps a mid-drag dialog that hasn't picked up
    // the `floating` class yet from insetting the viewer by
    // a rect that no longer starts at the edge.
    if (rect.width > 0 && rect.left <= 0) {
      value = `${Math.round(rect.right)}px`;
    }
  }
  if (bg.style.getPropertyValue('--viewer-inset-left') === value) {
    return false;
  }
  bg.style.setProperty('--viewer-inset-left', value);
  return true;
}

/**
 * Call `relayout()` on both viewers if they're mounted
 * and the method exists. Safe to call in tests where
 * the viewers haven't been constructed yet.
 *
 * The inset sync runs first: the viewers measure their own
 * containers, so the background's width has to be committed
 * before they read it.
 */
export function relayoutViewers(host) {
  syncViewerInset(host);
  const diff = host.shadowRoot?.querySelector('aic-diff-viewer');
  const svg = host.shadowRoot?.querySelector('aic-svg-viewer');
  if (diff && typeof diff.relayout === 'function') {
    diff.relayout();
  }
  if (svg && typeof svg.relayout === 'function') {
    svg.relayout();
  }
}