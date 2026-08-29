// Telling the server which file the user is looking at.
//
// `ClaudeCodeService.set_viewer_state` is the only writer of the server's
// `_viewer_state`, and that field feeds two readers: the `ui_state` MCP tool,
// and the fallback for a turn's `ViewerFraming` when `chat_streaming` is given
// no `viewer` argument — which is always. Until this module existed nothing
// called it from anywhere, so both readers spent the entire life of the app
// answering "nothing is open in the user's viewer pane". specs5/next.md § C7.
//
// One writer on purpose. The other arrival path — a `viewer` argument on
// `chat_streaming` — deliberately stays null. Two sources for one field is the
// shape specs5/next.md § C3 keeps finding: they disagree, neither is wrong, and
// the bug is unattributable. The push is also the arrival path that still works
// when the turn does not come from this browser at all, which is the only way
// the `ui_state` tool can be answered mid-session.
//
// What is *not* reported is the selection range. `set_viewer_state` accepts
// `start_line` / `end_line` and the framing renders them, but no selection
// plumbing exists in either viewer, and a range that lags the cursor is worse
// than no range — it points the agent at lines the user is not looking at. See
// specs5/next.md § C7.
//
// Governing specs:
//   - specs5/1-foundation/rpc-inventory.md § Who Calls These
//   - specs5/3-engine/session.md (turn framing)

// The SVG viewer synthesises this prefix for an ad-hoc comparison so the
// shell's visibility routing has a non-null path to act on. It is not a file
// the agent can read, so it is reported as nothing open rather than as itself.
const VIRTUAL_PREFIX = 'virtual://';

/**
 * Push `open` (a repo-relative path, or null to clear) to the server, unless
 * it is already what we last pushed.
 *
 * The dedupe is load-bearing, not an optimisation: `active-file-changed`
 * repeats for an unchanged path on purpose — the SVG viewer re-dispatches on a
 * same-file `openFile` precisely so the shell re-runs its visibility routing —
 * so every redundant open would otherwise cost a round trip to say nothing.
 *
 * `_reportedViewerPath` is only advanced once the call has come back, so a push
 * that fails mid-flight is retried by the next event rather than recorded as
 * done. Failures are swallowed: a non-localhost participant is refused by the
 * method's own gate, and framing the agent's prompt is not something to toast
 * about either way.
 */
async function pushViewerState(host, open) {
  // The field starts null and a fresh server holds null too, so an opening
  // report of "nothing open" dedupes away instead of being a wasted call.
  if (host._reportedViewerPath === open) return;
  if (!host.call) return;
  const setViewerState = host.call['ClaudeCodeService.set_viewer_state'];
  if (typeof setViewerState !== 'function') return;
  try {
    await setViewerState(open);
    host._reportedViewerPath = open;
  } catch (err) {
    console.debug('[app-shell] viewer state push failed', err);
  }
}

/**
 * A viewer has reported its active file. Decide what that means for what is
 * on screen, and tell the server.
 *
 * `source` is the viewer that emitted ('diff' or 'svg'). It matters in one
 * case: both viewers emit whenever *their* own file changes, so a hidden
 * viewer closing its last file must not report "nothing open" while the
 * visible viewer still has one. Any reported path, virtual or not, foregrounds
 * its viewer, so a report that carries one is always about what is on screen.
 */
export function reportViewerFile(host, path, source) {
  const reported = typeof path === 'string' ? path : '';
  if (!reported) {
    if (source !== host._activeViewer) return undefined;
    return pushViewerState(host, null);
  }
  if (reported.startsWith(VIRTUAL_PREFIX)) {
    // On screen, and becoming visible, but not a file anything can read.
    return pushViewerState(host, null);
  }
  return pushViewerState(host, reported);
}

/**
 * Re-push after a reconnect.
 *
 * A reconnect usually means the server process restarted, and `_viewer_state`
 * lives in memory — so the file the user still has open in front of them is a
 * file the new process has never been told about. Without this the agent goes
 * back to being told nothing until the user happens to open a different file.
 *
 * Clearing the record first is what stands the dedupe down for one push. It
 * also settles the nothing-was-open case on its own: the clear makes the
 * re-push a no-op instead of a call that would tell a fresh server what it
 * already believes.
 */
export function resendViewerState(host) {
  const open = host._reportedViewerPath;
  host._reportedViewerPath = null;
  return pushViewerState(host, open);
}
