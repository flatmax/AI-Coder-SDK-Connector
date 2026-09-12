"""Where ``agy`` keeps its configuration, and who owns that directory.

[AG-21](../../../specs5/plan-ag/decisions.md#ag-21) in one module. ``agy``
reads its **entire** configuration tree — hooks, MCP config, conversation
store, brain directory — from ``$HOME``, measured, and honours nothing
else: ``XDG_CONFIG_HOME`` loses to it, a repo-level ``.gemini/`` is not
read, and neither are three other spellings a hostile workspace might
plant. So the private config root this app has wanted since
[AG-18](../../../specs5/plan-ag/decisions.md#ag-18) costs one environment
variable, with no package, no privilege and nothing on the command line.

Two roots, and the reason is the hooks file
===========================================
The two ways this app runs ``agy`` want *opposite* contents in the same
``hooks.json``: the **master** engine needs tool calls allowed and routed
to the permission dialog, and the **consultant** needs them all denied.
One file cannot hold both, and a consultation running during a master turn
would have them fighting over it. So:

- :func:`master_root` — **stable**, under this app's own config directory,
  so resume keeps working across restarts while still being isolated from
  the user's ``~/.gemini``.
- :func:`ephemeral` — **one per consultation**, seeded and removed in a
  ``finally``. Concurrency becomes free and a consultation cannot litter
  the master's history or read it.

A third directory exists and is **not** a root: :func:`seed_cache`, which
holds a copy of the vendor's extracted helper binaries so that an ephemeral
root does not pay for them.

What the seed is, and what it is not
====================================
A bare root costs **17 MB and ~1 s** on first use, because ``agy``
re-extracts ``bin/`` and ``builtin/`` into every ``HOME`` it meets.
Symlinking those two directories brings the same root to **228 KB**,
measured 2026-09-11 — and that is the whole mechanism. The first version
of this design also shared ``XDG_CACHE_HOME``; isolating the two showed
``agy`` does not read it, creating ``$HOME/.cache/ms-playwright-go`` (an
empty marker directory) regardless. It is not in :data:`PASSTHROUGH` for
that reason, and the reason is written down because a compound
configuration credited to all of its parts is how the number was wrong the
first time.

**The symlinks point at a copy this app owns.** Linking straight into
``~/.gemini/antigravity-cli/bin`` was measured and did not write back. It
is still refused: "did not write this time" is not "cannot write", and a
vendor upgrade re-extracting its helpers would do so *through* the
symlink, into the exact tree the private root exists to stay out of.

What this is not
================
**It is not containment.** A hook is a callback inside the untrusted
binary; this module moves a directory. Policy holds while ``agy`` routes
every tool through ``PreToolUse`` and honours the verdict — measured
universal at v1.2.1, subagents included — and any code that *does* execute
can still reach ``/home/<user>`` by absolute path. ``bwrap`` remains the
optional hardening tier
([AG-20](../../../specs5/plan-ag/decisions.md#ag-20)), never a
prerequisite.

**It does not migrate anything.** Moving the master root leaves the
existing conversations under the user's own tree, unresumable by the
vendor and unreadable by the subagent scanner, but fully readable in the
UI because this app mirrors its own transcripts. That is
[AG-R-21](../../../specs5/plan-ag/risks.md#ag-r-21), and it is a decision
to accept rather than work to do. Hardlinking them across is **unsafe** —
every conversation database is ``journal_mode=wal`` and SQLite writes
through the inode, so a hardlink would reconnect the two roots this module
exists to separate.

Governing spec: ``specs5/plan-ag/`` — AG-21, AG-18; ``risks.md`` AG-R-18,
AG-R-21.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

#: The vendor's directory inside a config root. ``agy`` shares it with the
#: Gemini CLI, which is why it is not named after Antigravity.
VENDOR_DIR = ".gemini"

#: The product's directory inside that. Everything stateful lives here —
#: ``conversations/``, ``brain/``, ``bin/``, ``builtin/``.
PRODUCT_DIR = "antigravity-cli"

#: Our directory inside the app's config directory, holding every root we
#: own. One level of nesting so that ``uninstall`` and a user clearing
#: state have a single thing to remove.
ROOTS_DIR = "agy-roots"

#: The vendor directories worth seeding: extracted helper binaries and the
#: built-in skills. Both are **content-addressed by the vendor's own
#: version**, immutable for the life of an install, and together they are
#: the whole 17 MB.
SEEDED = ("bin", "builtin")

#: The environment ``agy`` is spawned with — this list and nothing else,
#: plus ``HOME``. An allowlist rather than a blocklist because the set of
#: credentials that must not leak into another vendor's agent is open
#: (``ANTHROPIC_API_KEY``, ``AWS_*``, whatever the user exports next) and
#: the set it needs is closed and short.
#:
#: ``DBUS_SESSION_BUS_ADDRESS`` and ``XDG_RUNTIME_DIR`` are **load-bearing,
#: not hygiene**: credentials live in the login keyring over the session
#: bus, so dropping them does not degrade auth, it removes it. Both were
#: measured to survive ``systemd-run --user --scope``, which is how the
#: cgroup identity in AG-18 coexists with this.
#:
#: The proxy and CA entries are here because a corporate network without
#: them looks exactly like an outage, and the display entries because a
#: locked keyring prompts.
PASSTHROUGH = (
    "PATH",
    "TMPDIR",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TERM",
    "USER",
    "LOGNAME",
    # Auth. See above — these are not optional.
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_RUNTIME_DIR",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    # Networks that are not the developer's.
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    # The vendor's own credentials, for the API-key path this app does not
    # use but the user's shell may be configured for. Nobody else's key is
    # on this list, which is the point of it.
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)


def user_root() -> Path:
    """The config root ``agy`` would use with no help from us.

    The user's real home, holding the tree their own interactive ``agy``
    writes to. Read for seeding and **never written** once a private root
    exists.
    """
    return Path.home()


def vendor_dir(root: Path | str) -> Path:
    """``<root>/.gemini/antigravity-cli`` — everything stateful."""
    return Path(root) / VENDOR_DIR / PRODUCT_DIR


def brain_dir(root: Path | str) -> Path:
    """Where ``agy`` writes generated images and subagent transcripts.

    The answer to [AG-R-18](../../../specs5/plan-ag/risks.md#ag-r-18): a
    function of the root in use, rather than a module constant frozen at
    import time from the *server's* ``HOME``. With two roots there is no
    single correct value for such a constant, and the failure it produced
    was silent — image collection and every subagent transcript looking
    under a directory the agent had never written to.
    """
    return vendor_dir(root) / "brain"


def hooks_file(root: Path | str) -> Path:
    """Where ``agy`` reads hooks inside ``root``.

    Measured as the only location loaded headlessly: a ``hooks.json`` at
    the repo level, at ``<root>/.gemini/hooks.json``, under ``.agy/`` or
    under ``.antigravity/`` is not read at v1.2.1.
    """
    return Path(root) / VENDOR_DIR / "config" / "hooks.json"


def mcp_config_file(root: Path | str) -> Path:
    """Where ``agy`` reads its MCP servers inside ``root``.

    The sibling of :func:`hooks_file`, and measured in the same place:
    ``$HOME/.gemini/config/mcp_config.json``. Since the root is this app's
    own, nothing else writes this file and it can be rewritten from
    scratch at every spawn.
    """
    return Path(root) / VENDOR_DIR / "config" / "mcp_config.json"


def write_mcp_config(root: Path | str, config: Mapping[str, object]) -> Path:
    """Write ``config`` as the root's MCP configuration. Returns the path.

    **This file holds a bearer token**, so three things are deliberate.

    It is written *atomically* — a temporary file in the same directory,
    then :func:`os.replace` — because ``agy`` reads it during startup and a
    truncated read is a session that silently has no consultant. Writing in
    place would put a window of a few bytes between two spawns.

    It is ``0600`` inside a ``0700`` directory, both set explicitly rather
    than left to whatever ``umask`` the app was launched under. A umask of
    ``0022`` is the common default and would make a credential
    world-readable.

    And it is written **unconditionally at every start**, overwriting
    whatever a previous run left. The alternative — blanking the file on
    exit — is a promise a crash does not keep, and a stale entry names a
    port this process no longer listens on with a token it no longer
    honours, which is a confusing failure rather than a safe one.
    """
    target = mcp_config_file(root)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        parent.chmod(0o700)
    handle, temporary = tempfile.mkstemp(dir=parent, prefix=".mcp_config-")
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2)
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return target


def clear_mcp_config(root: Path | str) -> bool:
    """Remove the root's MCP configuration. Returns whether there was one.

    The other half of "written unconditionally": a spawn that offers no
    consultant must leave no file behind claiming one, or ``agy`` spends
    its startup dialling a port that answers nothing and the user reads an
    MCP error for a feature that was never switched on.
    """
    try:
        mcp_config_file(root).unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("Could not remove the MCP config in %s: %s", root, exc)
        return False
    return True


def roots_dir(config_dir: Path | str) -> Path:
    """Every root this app owns, under its own config directory."""
    return Path(config_dir) / ROOTS_DIR


def master_root(config_dir: Path | str) -> Path:
    """The stable root for ``agy``-as-engine.

    Stable because the master resumes: ``--conversation <id>`` finds its
    database under the ``HOME`` it is given, so a root that changed between
    runs would make every previous conversation unreachable — which is
    precisely the one-time cost recorded as AG-R-21 for the *existing*
    conversations, and there is no reason to pay it repeatedly.
    """
    return roots_dir(config_dir) / "master"


def seed_cache(config_dir: Path | str) -> Path:
    """This app's copy of the vendor's extracted helpers.

    Not a config root — nothing is ever spawned against it. It holds only
    :data:`SEEDED`, so that :func:`seed` has something of ours to point at.
    """
    return roots_dir(config_dir) / "seed"


def ensure_seed(config_dir: Path | str, *, source: Path | str | None = None) -> Path | None:
    """Populate the seed cache from an existing install, once.

    Returns the cache when it holds something usable, and ``None`` when
    there is nothing to copy — a first run on a machine where ``agy`` has
    never been used. ``None`` is not an error: the caller simply skips
    seeding and the root pays the 17 MB it was always going to pay,
    once, and can be seeded from next time.

    Copied rather than linked, because the copy is what makes the
    ephemeral roots' symlinks safe to write through.
    """
    cache = seed_cache(config_dir)
    origin = vendor_dir(source if source is not None else user_root())
    present = [name for name in SEEDED if (cache / name).is_dir()]
    if len(present) == len(SEEDED):
        return cache
    for name in SEEDED:
        target = cache / name
        if target.is_dir():
            continue
        available = origin / name
        if not available.is_dir():
            continue
        try:
            cache.mkdir(parents=True, exist_ok=True)
            # Into a sibling and renamed, so an interrupted copy never
            # leaves a half-populated directory that later runs would
            # treat as complete and symlink into every root.
            staging = Path(tempfile.mkdtemp(prefix=f".{name}-", dir=cache))
            shutil.copytree(available, staging / name, symlinks=True)
            os.replace(staging / name, target)
            staging.rmdir()
        except OSError as exc:
            logger.warning("Could not seed %s from %s: %s", target, available, exc)
    return cache if any((cache / name).is_dir() for name in SEEDED) else None


def seed(
    root: Path | str, config_dir: Path | str, *, source: Path | str | None = None
) -> list[str]:
    """Link the seeded directories into ``root``. Returns what was linked.

    Silent and partial by design: a seed that cannot be made is a slower
    first run, not a failure, and raising here would turn a 17 MB tax into
    a consultation that does not happen.

    ``source`` is where the vendor's extracted helpers are copied from the
    first time, defaulting to the user's own install. It is threaded rather
    than assumed so that a caller can say *which* tree it means — which
    matters most to the test suite, where an unstated default would read
    the developer's real ``~/.gemini`` and copy 17 MB per test.
    """
    cache = ensure_seed(config_dir, source=source)
    if cache is None:
        return []
    destination = vendor_dir(root)
    linked: list[str] = []
    for name in SEEDED:
        available = cache / name
        target = destination / name
        if not available.is_dir() or target.exists() or target.is_symlink():
            continue
        try:
            destination.mkdir(parents=True, exist_ok=True)
            target.symlink_to(available, target_is_directory=True)
            linked.append(name)
        except OSError as exc:
            logger.warning("Could not link %s into %s: %s", name, destination, exc)
    return linked


def prepare(
    root: Path | str, config_dir: Path | str, *, source: Path | str | None = None
) -> Path:
    """Make ``root`` exist, seeded and ready to be spawned against."""
    root = Path(root)
    vendor_dir(root).mkdir(parents=True, exist_ok=True)
    hooks_file(root).parent.mkdir(parents=True, exist_ok=True)
    seed(root, config_dir, source=source)
    return root


#: Whether :func:`sweep_consultations` has already run in this process.
#: A set rather than a module-level ``bool`` so the write is an attribute
#: mutation and not a rebinding, which keeps it correct without a
#: ``global`` statement and visible to anything that imported the module.
_SWEPT: set[bool] = set()


@contextmanager
def ephemeral(
    config_dir: Path | str,
    *,
    prefix: str = "c-",
    source: Path | str | None = None,
) -> Iterator[Path]:
    """A private root for the length of one consultation.

    Removed in a ``finally``, which is safe rather than hopeful: measured
    2026-09-11, no process survives ``agy``'s exit holding the private
    ``HOME`` — checked through ``/proc/*/environ`` — and ``rmtree`` on the
    root immediately afterwards raises nothing.

    **The caller must have stopped the session before this block exits**,
    and every caller does: :meth:`AgyConsultant._run` closes the session in
    its own ``finally``, which closes the process *and* the gate, and the
    gate stops the ``systemd`` scope the children were placed in. That
    ordering is the reason the measurement above holds — it is not a
    property of ``agy`` exiting politely, it is a property of the scope
    being torn down first. Reversing the two would leave `rmtree` deleting
    a tree a live process still has open, which on Linux succeeds silently
    and leaves the vendor writing into unlinked inodes.

    Removal failures are logged and swallowed. A consultation that
    succeeded and then could not tidy up has still succeeded, and the
    directory is 228 KB inside a directory this app already owns.
    """
    parent = roots_dir(config_dir) / "consultations"
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    try:
        yield prepare(root, config_dir, source=source)
    finally:
        try:
            shutil.rmtree(root)
        except OSError as exc:
            logger.warning("Could not remove the consultation root %s: %s", root, exc)


def sweep_consultations(config_dir: Path | str) -> int:
    """Remove consultation roots a previous process left behind.

    Returns how many went. :func:`ephemeral` removes its own in a
    ``finally``, so the only way one survives is a process that did not get
    to run it — ``SIGKILL``, a power cut, an ``os._exit``. Nothing collects
    those otherwise, and each is a small pile of symlinks and a hooks file
    naming a socket that no longer exists.

    **Once per process, and that is load-bearing rather than an
    optimisation.** Nothing here is filtered by age, because age is not the
    question — a root belongs to one ``with`` block in one process, so
    before this process has opened any, everything present is somebody
    else's litter. After it has opened one, that reasoning stops being
    true, and a second call would delete a live consultation's root out
    from under it. Hence the latch: the safe moment is the first one, and
    there is only one of it.
    """
    if _SWEPT:
        return 0
    _SWEPT.add(True)
    parent = roots_dir(config_dir) / "consultations"
    if not parent.is_dir():
        return 0
    swept = 0
    for child in sorted(parent.iterdir()):
        if not child.is_dir():
            continue
        try:
            shutil.rmtree(child)
        except OSError as exc:
            logger.warning("Could not remove the stale consultation root %s: %s", child, exc)
            continue
        swept += 1
    if swept:
        logger.info("Removed %d consultation root(s) left by a previous run", swept)
    return swept


def environment(
    root: Path | str, *, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The environment to spawn ``agy`` with, and nothing more.

    ``HOME`` is the private root; everything else is whatever
    :data:`PASSTHROUGH` finds in ``base``. Absent names are omitted rather
    than set empty, because an empty ``HTTPS_PROXY`` is not the same
    statement as an unset one.

    **This is the containment, such as it is.** The root moves the vendor's
    files; the allowlist decides what it can read out of this process. A
    spawn that inherited the parent's environment would hand another
    vendor's agent every credential the developer has exported, and the
    root would have done nothing about it.
    """
    source = os.environ if base is None else base
    env = {name: source[name] for name in PASSTHROUGH if source.get(name)}
    env["HOME"] = str(Path(root))
    return env
