// Absolute paths the engine reports, turned into the repo-relative paths
// every `Repo` method takes.
//
// Two facts that have to meet somewhere. Claude Code's file tools require
// absolute paths, so a tool card's `file_path` — and the `files_modified`
// list built from it — is absolute, deliberately and by documented contract
// (`specs5/plan/README.md`). And every `Repo` RPC takes a path relative to
// the repo root, rejecting an absolute one outright rather than resolving it,
// because resolving would be a way around the containment check
// (`src/aic_dc/repo/paths.py`).
//
// Nothing used to convert between them, so clicking a tool card's file chip
// asked the backend for `/home/you/repo/tests/thing.py`, got
// `Absolute paths not accepted` on the server's stderr, and left the viewer
// empty with nothing on screen to explain it.
//
// The root arrives once, in the shell's state snapshot (`repo_root`). This
// module is where the conversion lives so that it is one rule rather than a
// startsWith per caller.
//
// It is also where the root itself lives, for the reason `rpc.js` gives for
// the call proxy: there is one shell, it learns the root once, and the
// alternative is threading a string through the DOM to renderers that take a
// path and nothing else. `setRepoRoot` has exactly one caller
// (`app-shell/state-fetch.js`) — a second writer would be two answers to
// "where is the repo", which is the shape `specs5/next.md` § C3 keeps finding.
//
// **The same rule serves display**, which is why there is no second function
// for it (`next.md` § C4). A chip label wants the repo-relative name when
// there is one and the absolute path when there is not, because a file
// outside the root has no other name — and that is `toRepoPath` exactly. The
// house rule it implements was already written down for the Context tab's
// memory-file table: named relative to the root, with the engine's absolute
// path on the tooltip.

/**
 * True for a POSIX absolute path or a Windows drive-letter path.
 *
 * Mirrors the backend's own test (`paths.py` checks for a leading slash or a
 * colon in position 1) so that "the browser thinks this is absolute" and
 * "the backend will refuse this" cannot disagree.
 */
function isAbsolute(path) {
  return path.startsWith('/') || (path.length >= 2 && path[1] === ':');
}

/**
 * The repo's absolute root, or `''` before the first snapshot lands.
 *
 * Empty rather than null so `toRepoPath`'s guard is one truthiness test:
 * "no root yet" and "a backend that never sends one" behave identically,
 * and both leave an absolute path exactly as it was.
 */
let repoRoot = '';

/**
 * Publish the repo root. One caller: the shell's state-snapshot handler.
 *
 * A non-string or empty value is ignored rather than stored, so a snapshot
 * from a backend that does not carry `repo_root` cannot un-set a root a
 * previous one established. Reconnects re-deliver the same snapshot, and a
 * reconnect to a *different* repo replaces the value, which is correct.
 */
export function setRepoRoot(root) {
  if (typeof root !== 'string' || !root) return;
  repoRoot = root;
}

/** The published root. Exported for assertions; production code passes it
 * implicitly by omitting `toRepoPath`'s second argument. */
export function getRepoRoot() {
  return repoRoot;
}

/**
 * Test hook — forget the root. Production code never calls this.
 *
 * Module state outlives a test case, so without this a test that publishes
 * a root changes the answer for every test that runs after it. Same reason
 * `SharedRpc.reset()` exists.
 */
export function resetRepoRoot() {
  repoRoot = '';
}

/**
 * `path` expressed relative to `root`, when it is inside it.
 *
 * Returns `path` untouched when it is already relative, when there is no
 * root to measure against, or when it points outside the repo. That last
 * case is not a fallback so much as a deliberate non-answer: a file outside
 * the repo root has no repo-relative name, and the RPC refusing it is the
 * correct outcome rather than something to paper over with `../..`.
 *
 * Non-string input passes through so callers can normalise before their own
 * type guard runs, and keep that guard as the single place a bad path stops.
 *
 * `root` defaults to the published one, which is what every caller wants;
 * it stays a parameter so the rule itself can be tested without module
 * state, and so a caller that genuinely holds a different root can say so.
 */
export function toRepoPath(path, root = repoRoot) {
  if (typeof path !== 'string' || !path) return path;
  if (!isAbsolute(path)) return path;
  if (typeof root !== 'string' || !root) return path;
  // A trailing slash on the root is the difference between `tests/a.py` and
  // `/tests/a.py`, and the second is absolute again.
  const base = root.replace(/\/+$/, '');
  if (!base || !path.startsWith(`${base}/`)) return path;
  const relative = path.slice(base.length + 1);
  // `${base}/` with nothing after it is the root directory, not a file in it.
  return relative || path;
}
