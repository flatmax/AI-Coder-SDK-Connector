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
 */
export function toRepoPath(path, root) {
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
