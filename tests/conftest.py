"""Fixtures every test in this suite gets, and none of them is optional.

Two of the three are about the developer's own home directory, which the
``agy`` code reaches into by design: one keeps the suite from *reading* it
and one keeps the suite from *deleting* out of it. The third undoes a
once-per-process latch that a test process runs into hundreds of times.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_seeding_from_the_developers_home(monkeypatch, tmp_path_factory):
    """Keep the seed cache from reading the user's real ``~/.gemini``.

    AG-21 gives ``agy`` a private config root, and a bare root costs 17 MB
    because the vendor re-extracts ``bin/`` and ``builtin/`` into every
    ``HOME`` it meets. :func:`aic_dc.agy.roots.seed` avoids that by
    symlinking a copy this app owns — and the first time, it makes that
    copy from wherever the vendor already extracted them, which in
    production is the user's own home.

    In a test process that default is wrong twice over. It **reads the
    developer's real configuration**, which is the machine-dependence this
    suite is otherwise careful about, and it copies 17 MB per test that
    prepares a root — measured at 646 MB of ``/tmp`` across one run before
    this fixture existed.

    Pointed at an empty directory instead, where ``ensure_seed`` finds
    nothing to copy and says so by returning ``None``. That is a supported
    answer rather than a stubbed one: it is exactly what a first run on a
    machine where ``agy`` has never been used produces, and the root simply
    pays for its own extraction. Tests that are *about* seeding pass an
    explicit ``source=`` and are unaffected.
    """
    from aic_dc.agy import roots

    empty = tmp_path_factory.mktemp("no-agy-install")
    monkeypatch.setattr(roots, "user_root", lambda: empty)


@pytest.fixture(autouse=True)
def _one_sweep_per_test_not_per_process():
    """Reset the latch :func:`aic_dc.agy.roots.sweep_consultations` holds.

    That function deletes every consultation root it finds, unfiltered by
    age, and is safe only because it runs before this process has opened
    one of its own. A module-level latch enforces "only the first call",
    which is right for a server that starts once and then runs for days.

    A test process is hundreds of notional processes in one, so the latch
    survives into tests that had nothing to do with the one that tripped
    it — and a sweep that returns ``0`` because somebody else already swept
    is indistinguishable, from inside a test, from a sweep that found
    nothing. Clearing it per test restores the thing the production code is
    actually claiming: each process sweeps once.
    """
    from aic_dc.agy import roots

    roots._SWEPT.clear()
    yield
    roots._SWEPT.clear()


@pytest.fixture(autouse=True)
def _no_uninstalling_from_the_developers_home(monkeypatch, tmp_path_factory):
    """Point :data:`aic_dc.agy.install.GLOBAL_HOOKS` somewhere disposable.

    That constant is ``Path.home() / ".gemini" / "config" / "hooks.json"``,
    resolved at **import** time against the real home, and AG-21 gave the
    connect path a reason to write to it: ``install.retire_global()``
    removes the pre-AG-21 entry, unasked, because the app that installed it
    no longer reads it.

    Correct in production and destructive in a test process, which connects
    hundreds of times. **This was not hypothetical** — the run that
    introduced ``retire_global`` deleted the developer's real
    ``~/.gemini/config/hooks.json``. Nothing of theirs was lost, because
    :func:`aic_dc.agy.install.uninstall` removes the file only when our
    entry was the last thing in it, but that is luck about the file's
    contents rather than a property of the suite.

    Tests that are *about* the legacy entry monkeypatch the same constant
    to a path they control, which simply wins over this one.
    """
    from aic_dc.agy import install

    monkeypatch.setattr(
        install,
        "GLOBAL_HOOKS",
        tmp_path_factory.mktemp("no-real-global-hooks") / "hooks.json",
    )
