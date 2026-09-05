"""Shared setup for the ``agy`` live probes, and one trap they all share.

Why this file exists: **an ``agy`` probe that works in an untrusted
directory proves nothing about writes.**

``agy`` refuses to write outside ``trustedWorkspaces`` in
``~/.gemini/antigravity-cli/settings.json`` — but it does not *fail*. It
writes the file into ``~/.gemini/antigravity-cli/scratch/`` instead and
reports success, which is
[`risks.md` AG-R-3](../specs5/plan-ag/risks.md#ag-r-3). Measured again on
2026-09-05, when ``probe_agy_isolation.py`` ran in ``/tmp/agy-iso-theirs-…``
and its file landed in ``scratch/`` while the run reported ``SUCCESS``.

That is merely confusing for a probe asserting a write *happened*. It is
much worse for one asserting a write **did not** happen: under diversion
the target file is unchanged no matter what the permission gate did, so a
deny tripwire in an untrusted directory passes whether or not the gate
works. ``probe_agy_gate.py`` was written that way, and its recorded PASS
therefore rested on an assumption nobody had checked. :func:`probe_root`
is what closes that, and it **raises rather than warns**, because the
failure it prevents is a green test that means nothing.

``/tmp`` itself is typically *not* trusted while ``/tmp/temp`` is, so the
distinction is easy to miss by one path component.

A trusted root is necessary and, on its own, was not sufficient
-----------------------------------------------------------------
Three runs on 2026-09-05 diverted a write from inside ``/tmp/temp``, which
*is* trusted — twice from a plain directory and once from a
git-initialised one, so repository-ness is not the difference either.

What every diverted file ever recorded has in common is that it was
**newly created**: ``probe.txt`` (2026-08-30), ``hello.txt`` and
``test_hello_world.py`` (2026-09-04), and ``stranger.txt`` on all three
runs here. Against that, the same day's browser demonstration *edited an
existing* file at ``/tmp/temp/agy-write-test/target.txt`` and the edit
landed on disk.

So the working reading is that a bare filename handed to ``write_to_file``
is not resolved against the session's cwd, and "untrusted workspace" was
only ever the most visible correlate. **This is a reading of the evidence,
not a measured rule** — it has not been isolated with a controlled probe,
and the alternative that creation and modification are trusted differently
is not excluded. Probes that need a write to land should therefore **seed
the file and ask for an edit**, which is what the two live probes do.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

SETTINGS = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"


def trusted_workspaces() -> list[Path]:
    """The roots ``agy`` will actually write inside. Empty if unreadable."""
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("trustedWorkspaces")
    if not isinstance(entries, list):
        return []
    roots = []
    for entry in entries:
        if isinstance(entry, str) and entry.strip():
            try:
                roots.append(Path(entry.strip()).resolve())
            except (OSError, ValueError):
                continue
    return roots


def is_trusted(path: Path) -> bool:
    """Whether ``agy`` would write here rather than divert to ``scratch/``."""
    try:
        target = Path(path).resolve()
    except (OSError, ValueError):
        return False
    for root in trusted_workspaces():
        if target == root or root in target.parents:
            return True
    return False


def _git_init(root: Path) -> None:
    """Make the probe directory a git repository.

    Kept because a real ``agy`` session is normally in a repo, so the
    probes should look like one. **It is not the fix for the diversion**,
    and that is worth recording because it was tried as one: a
    git-initialised directory under the trusted ``/tmp/temp`` had its
    newly-created file diverted to ``scratch/`` exactly as the plain
    directory had. See this module's docstring for what the evidence
    actually points at.
    """
    try:
        subprocess.run(
            ["git", "init", "-q", str(root)],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        # Not fatal on its own: the caller's assertions still run, and a
        # diverted write shows up as the probe's own loud failure.
        pass


def probe_root(prefix: str) -> Path:
    """A fresh temporary directory ``agy`` will really write into.

    **Only under the system temp directory**, never in an arbitrary
    trusted workspace. The trusted list is the user's own working roots —
    on this machine it names four real project repositories and ``$HOME``
    — and an agent told to edit a file, running loose in one of those, is
    not something a probe should arrange. The first version of this
    function took the first trusted root it found and picked a live
    project; that is how it reads when a helper optimises for "somewhere
    that works".

    Raises rather than falling back to an untrusted directory, because
    every assertion these probes make about a file on disk is void outside
    a trusted workspace: a deny tripwire passes on a diverted write, and
    an allow probe fails on one.
    """
    roots = trusted_workspaces()
    if not roots:
        raise RuntimeError(
            f"Could not read trustedWorkspaces from {SETTINGS}. agy diverts "
            f"writes outside those roots into its scratch directory while "
            f"reporting success (AG-R-3), so a probe cannot assert anything "
            f"about a file until this is readable."
        )
    tmp = Path(tempfile.gettempdir()).resolve()
    for candidate in roots:
        if candidate != tmp and tmp not in candidate.parents:
            continue
        if not candidate.is_dir():
            continue
        try:
            root = Path(tempfile.mkdtemp(prefix=prefix, dir=candidate))
        except OSError:
            continue
        _git_init(root)
        return root
    raise RuntimeError(
        f"agy trusts {[str(r) for r in roots]}, but none of those is a "
        f"writable directory under {tmp}. These probes will not run inside "
        f"a real workspace, so add a scratch one — e.g. `mkdir -p "
        f"{tmp / 'temp'}` and add it to trustedWorkspaces in {SETTINGS}."
    )
