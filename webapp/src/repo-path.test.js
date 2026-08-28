// Tests for webapp/src/repo-path.js — the one conversion between the
// absolute paths the engine reports for tool calls and the repo-relative
// paths every `Repo` RPC takes.
//
// The rule being pinned is narrow on purpose: relativise what is inside the
// root, and hand back everything else untouched. A helper that guessed —
// stripping a leading slash, or walking up with `../` — would turn a refused
// request into a request for the wrong file.

import { beforeEach, describe, expect, it } from 'vitest';
import {
  getRepoRoot,
  resetRepoRoot,
  setRepoRoot,
  toRepoPath,
} from './repo-path.js';

const ROOT = '/home/dev/my-repo';

// The root is module state, so it outlives a test case. Every test below
// either passes a root explicitly or expects none to be published; without
// this, one that publishes would decide the answer for the rest of the file.
beforeEach(() => {
  resetRepoRoot();
});

describe('toRepoPath', () => {
  it('relativises a path inside the repo', () => {
    expect(toRepoPath(`${ROOT}/tests/test_thing.py`, ROOT))
      .toBe('tests/test_thing.py');
    expect(toRepoPath(`${ROOT}/README.md`, ROOT)).toBe('README.md');
  });

  it('leaves an already-relative path alone', () => {
    expect(toRepoPath('src/main.js', ROOT)).toBe('src/main.js');
    expect(toRepoPath('README.md', ROOT)).toBe('README.md');
  });

  it('tolerates a trailing slash on the root', () => {
    // Left in, this is the difference between `a.js` and `/a.js`, and the
    // second is absolute again — which is what the backend rejects.
    expect(toRepoPath(`${ROOT}/a.js`, `${ROOT}/`)).toBe('a.js');
    expect(toRepoPath(`${ROOT}/a.js`, `${ROOT}///`)).toBe('a.js');
  });

  it('leaves a path outside the repo absolute', () => {
    // No repo-relative name exists for it, and `Repo` refusing it is the
    // right answer rather than something to paper over with `../..`.
    expect(toRepoPath('/etc/passwd', ROOT)).toBe('/etc/passwd');
    expect(toRepoPath('/home/dev/other-repo/a.js', ROOT))
      .toBe('/home/dev/other-repo/a.js');
  });

  it('does not mistake a sibling with a shared prefix for a child', () => {
    // `/home/dev/my-repo-backup` starts with the root as a *string* and is
    // not inside it. Matching on the separator is what makes that a miss.
    expect(toRepoPath(`${ROOT}-backup/a.js`, ROOT))
      .toBe(`${ROOT}-backup/a.js`);
    expect(toRepoPath(`${ROOT}x/a.js`, ROOT)).toBe(`${ROOT}x/a.js`);
  });

  it('leaves the root itself alone', () => {
    // The root is a directory, not a file in itself, and '' is not a path.
    expect(toRepoPath(ROOT, ROOT)).toBe(ROOT);
    expect(toRepoPath(`${ROOT}/`, ROOT)).toBe(`${ROOT}/`);
  });

  it('passes the path through when no root is known', () => {
    // The state snapshot has not landed yet: unchanged is the old
    // behaviour, and measuring against '' would produce a wrong path.
    // `undefined` takes the default, which the beforeEach has just cleared,
    // so all three arrive at the same guard.
    const abs = `${ROOT}/a.js`;
    expect(toRepoPath(abs, '')).toBe(abs);
    expect(toRepoPath(abs, undefined)).toBe(abs);
    expect(toRepoPath(abs, null)).toBe(abs);
  });

  it('passes non-string and empty input through to the caller guard', () => {
    expect(toRepoPath(undefined, ROOT)).toBe(undefined);
    expect(toRepoPath(null, ROOT)).toBe(null);
    expect(toRepoPath(42, ROOT)).toBe(42);
    expect(toRepoPath('', ROOT)).toBe('');
  });

  it('handles a Windows drive-letter root the same way', () => {
    // The backend calls a path with a colon in position 1 absolute, so this
    // helper has to agree with it about what needs converting.
    expect(toRepoPath('C:/repo/src/a.js', 'C:/repo')).toBe('src/a.js');
    expect(toRepoPath('D:/elsewhere/a.js', 'C:/repo'))
      .toBe('D:/elsewhere/a.js');
  });
});

// The module holds the root as well as the rule, so that the chip renderers
// — which take a path and no host — can label a path without one being
// threaded down to them, and so one repo has one answer.
describe('the published root', () => {
  it('is empty until a snapshot publishes one', () => {
    expect(getRepoRoot()).toBe('');
  });

  it('is what toRepoPath measures against when no root is passed', () => {
    setRepoRoot(ROOT);
    expect(getRepoRoot()).toBe(ROOT);
    expect(toRepoPath(`${ROOT}/src/a.js`)).toBe('src/a.js');
    expect(toRepoPath('/etc/passwd')).toBe('/etc/passwd');
  });

  it('cannot be un-set by a snapshot that omits it', () => {
    // An older backend sends no `repo_root`. Storing that would make every
    // absolute path stop resolving for the rest of the session, which is
    // worse than the reconnect having told us nothing new.
    setRepoRoot(ROOT);
    setRepoRoot(undefined);
    setRepoRoot(null);
    setRepoRoot('');
    setRepoRoot(42);
    expect(getRepoRoot()).toBe(ROOT);
    expect(toRepoPath(`${ROOT}/src/a.js`)).toBe('src/a.js');
  });

  it('is replaced when the snapshot names a different repo', () => {
    // Reconnecting to another server is a real case, and the last snapshot
    // is the authority — not the first one to arrive.
    setRepoRoot(ROOT);
    setRepoRoot('/home/dev/other');
    expect(getRepoRoot()).toBe('/home/dev/other');
    expect(toRepoPath('/home/dev/other/a.js')).toBe('a.js');
    expect(toRepoPath(`${ROOT}/a.js`)).toBe(`${ROOT}/a.js`);
  });

  it('is overridden by an explicit root argument', () => {
    // The parameter is what lets the rule be tested without module state.
    setRepoRoot(ROOT);
    expect(toRepoPath('/elsewhere/a.js', '/elsewhere')).toBe('a.js');
    expect(toRepoPath(`${ROOT}/a.js`, '/elsewhere')).toBe(`${ROOT}/a.js`);
  });

  it('is forgotten by resetRepoRoot', () => {
    // The test hook, for the same reason `SharedRpc.reset()` exists.
    setRepoRoot(ROOT);
    resetRepoRoot();
    expect(getRepoRoot()).toBe('');
    expect(toRepoPath(`${ROOT}/a.js`)).toBe(`${ROOT}/a.js`);
  });
});
