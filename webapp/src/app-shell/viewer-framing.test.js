// Tests for webapp/src/app-shell/viewer-framing.js — the only writer of
// the server's `_viewer_state`, which is the only source for the `ui_state`
// MCP tool's `viewer` key and the fallback for a turn's framing.
//
// The failure this guards is silence: nothing throws when the push stops
// happening, and nothing in the UI changes. The agent just goes back to
// being told nothing about what the user is looking at, which is the state
// the app shipped in until specs5/next.md § C7.

import { describe, expect, it, vi } from 'vitest';

import { reportViewerFile, resendViewerState } from './viewer-framing.js';
import { onActiveFileChanged } from './viewers.js';

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

function makeHost({ activeViewer = 'diff', reported = null } = {}) {
  const setViewerState = vi.fn().mockResolvedValue({ status: 'ok' });
  return {
    _activeViewer: activeViewer,
    _reportedViewerPath: reported,
    call: { 'ClaudeCodeService.set_viewer_state': setViewerState },
    setViewerState,
  };
}

describe('reportViewerFile', () => {
  it('pushes the path the visible viewer reports', async () => {
    const host = makeHost();
    await reportViewerFile(host, 'src/main.py', 'diff');
    expect(host.setViewerState).toHaveBeenCalledWith('src/main.py');
    expect(host._reportedViewerPath).toBe('src/main.py');
  });

  it('pushes a path from a viewer that is not visible yet', async () => {
    // The shell foregrounds whichever viewer reports a file, so the
    // report arrives before `_activeViewer` catches up.
    const host = makeHost({ activeViewer: 'diff' });
    await reportViewerFile(host, 'diagram.svg', 'svg');
    expect(host.setViewerState).toHaveBeenCalledWith('diagram.svg');
  });

  it('does not push the same path twice', async () => {
    const host = makeHost({ reported: 'src/main.py' });
    await reportViewerFile(host, 'src/main.py', 'diff');
    expect(host.setViewerState).not.toHaveBeenCalled();
  });

  it('pushes again when the path changes', async () => {
    const host = makeHost({ reported: 'src/main.py' });
    await reportViewerFile(host, 'src/other.py', 'diff');
    expect(host.setViewerState).toHaveBeenCalledWith('src/other.py');
    expect(host._reportedViewerPath).toBe('src/other.py');
  });

  it('clears when the visible viewer closes its last file', async () => {
    const host = makeHost({ activeViewer: 'diff', reported: 'src/main.py' });
    await reportViewerFile(host, null, 'diff');
    expect(host.setViewerState).toHaveBeenCalledWith(null);
    expect(host._reportedViewerPath).toBeNull();
  });

  it('ignores a hidden viewer closing its last file', async () => {
    // The diff viewer emptying while an SVG is on screen must not tell
    // the server nothing is open — something is.
    const host = makeHost({ activeViewer: 'svg', reported: 'diagram.svg' });
    await reportViewerFile(host, null, 'diff');
    expect(host.setViewerState).not.toHaveBeenCalled();
    expect(host._reportedViewerPath).toBe('diagram.svg');
  });

  it('reports a virtual comparison as nothing open', async () => {
    // `virtual://svg-compare/...` is not a file the agent can read, and
    // leaving the previous path standing would point it at a file that
    // is no longer on screen.
    const host = makeHost({ reported: 'src/main.py' });
    await reportViewerFile(host, 'virtual://svg-compare/right', 'svg');
    expect(host.setViewerState).toHaveBeenCalledWith(null);
  });

  it('does not record a push that failed', async () => {
    const host = makeHost({ reported: 'src/main.py' });
    host.call['ClaudeCodeService.set_viewer_state'] =
      vi.fn().mockRejectedValue(new Error('disconnected'));
    const debug = vi.spyOn(console, 'debug').mockImplementation(() => {});
    await reportViewerFile(host, 'src/other.py', 'diff');
    expect(host._reportedViewerPath).toBe('src/main.py');
    expect(debug).toHaveBeenCalled();
    debug.mockRestore();
  });

  it('is a no-op before the RPC proxy exists', async () => {
    const host = makeHost();
    host.call = null;
    await expect(
      reportViewerFile(host, 'src/main.py', 'diff'),
    ).resolves.toBeUndefined();
    expect(host._reportedViewerPath).toBeNull();
  });

  it('declines cleanly when the server does not expose the method', async () => {
    // An older server is a no-op, not a caught crash — without the
    // typeof guard the call still fails safe, but it fails through the
    // catch and logs, which is noise on every file the user opens.
    const host = makeHost();
    host.call = {};
    const debug = vi.spyOn(console, 'debug').mockImplementation(() => {});
    await reportViewerFile(host, 'src/main.py', 'diff');
    expect(host._reportedViewerPath).toBeNull();
    expect(debug).not.toHaveBeenCalled();
    debug.mockRestore();
  });
});

describe('the active-file-changed wiring', () => {
  // The push is only useful if the shell's handler still makes it. Nothing
  // else in the app reads `_reportedViewerPath`, so an unwired module would
  // pass every test above and report nothing.
  function viewerEvent(path, tagName) {
    return {
      detail: { path },
      composedPath: () => [{ tagName }],
    };
  }

  it('reports a diff-viewer file the shell has just routed to', async () => {
    const host = makeHost();
    host._saveViewportState = vi.fn();
    onActiveFileChanged(host, viewerEvent('src/main.py', 'AIC-DIFF-VIEWER'));
    await tick();
    expect(host.setViewerState).toHaveBeenCalledWith('src/main.py');
    expect(host._activeViewer).toBe('diff');
  });

  it('reports the close when the visible viewer empties', async () => {
    const host = makeHost({ reported: 'src/main.py' });
    host._saveViewportState = vi.fn();
    onActiveFileChanged(host, viewerEvent(null, 'AIC-DIFF-VIEWER'));
    await tick();
    expect(host.setViewerState).toHaveBeenCalledWith(null);
    // The null path still must not disturb viewer visibility.
    expect(host._saveViewportState).not.toHaveBeenCalled();
  });

  it('reports nothing when the emitter is not a viewer', async () => {
    const host = makeHost();
    host._saveViewportState = vi.fn();
    onActiveFileChanged(host, viewerEvent('src/main.py', 'DIV'));
    await tick();
    expect(host.setViewerState).not.toHaveBeenCalled();
  });
});

describe('resendViewerState', () => {
  it('re-pushes the open file after a reconnect', async () => {
    // A restarted server holds no viewer state, so the dedupe has to be
    // stood down for one push or the agent is told nothing until the
    // user opens a different file.
    const host = makeHost({ reported: 'src/main.py' });
    await resendViewerState(host);
    expect(host.setViewerState).toHaveBeenCalledWith('src/main.py');
    expect(host._reportedViewerPath).toBe('src/main.py');
  });

  it('sends nothing when no file was open', async () => {
    const host = makeHost({ reported: null });
    await resendViewerState(host);
    expect(host.setViewerState).not.toHaveBeenCalled();
  });
});
