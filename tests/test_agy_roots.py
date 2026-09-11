"""Tests for the private ``agy`` config root — AG-21 and AG-R-18.

The module under test is ten short functions over path arithmetic, so
asserting that they join strings correctly would be a tautology. What is
worth testing is the three things the design actually turns on, each of
which was a position that changed under measurement:

**The root is the whole configuration.** ``agy`` reads hooks, MCP config,
the conversation store and the brain tree from ``$HOME`` and nothing else,
so every path this module produces has to hang off one directory. A
function that reached for ``Path.home()`` would work perfectly on a
developer's machine with one root and be wrong the moment there were two —
which is AG-R-18, and it is silent.

**The seed is symlinks, not a copy.** A bare root costs 17 MB because the
vendor re-extracts its helpers into every ``HOME``; symlinking two
directories brings it to 228 KB. The symlinks point at a copy this app
owns rather than at the user's tree, and that distinction is load-bearing
rather than fastidious: a vendor upgrade re-extracting through a symlink
would write into the exact directory the private root exists to stay out
of.

**The environment is an allowlist.** The root moves the vendor's files;
the allowlist decides what it can read out of this process. A spawn that
inherited the environment would hand another vendor's agent every
credential the developer has exported, and the root would have done
nothing about it.

Offline. Never reads or writes the real ``~/.gemini`` — see the autouse
fixture in ``tests/conftest.py`` for why that needs saying.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aic_dc.agy import roots


@pytest.fixture
def vendor_install(tmp_path):
    """A tree shaped like a real ``agy`` install, to seed from."""
    home = tmp_path / "their-home"
    vendor = roots.vendor_dir(home)
    for name in roots.SEEDED:
        (vendor / name).mkdir(parents=True)
        (vendor / name / f"{name}-payload").write_text("x", encoding="utf-8")
    return home


class TestEveryPathHangsOffTheRoot:
    """AG-R-18 as arithmetic: no path here may consult the process.

    The constants these replaced were evaluated at import time from the
    server's own ``HOME``, which is the one root no session ever runs
    against once AG-21 exists.
    """

    @pytest.mark.parametrize(
        "accessor",
        [roots.vendor_dir, roots.brain_dir, roots.hooks_file],
    )
    def test_it_is_under_the_root_it_was_given(self, tmp_path, accessor):
        assert accessor(tmp_path).is_relative_to(tmp_path)

    @pytest.mark.parametrize(
        "accessor",
        [roots.vendor_dir, roots.brain_dir, roots.hooks_file],
    )
    def test_two_roots_give_two_answers(self, tmp_path, accessor):
        assert accessor(tmp_path / "a") != accessor(tmp_path / "b")

    def test_the_hooks_file_is_the_one_location_agy_reads(self, tmp_path):
        """Measured at v1.2.1: a ``hooks.json`` at the repo level, at
        ``<root>/.gemini/hooks.json``, under ``.agy/`` or under
        ``.antigravity/`` is not loaded. Only this path is."""
        assert roots.hooks_file(tmp_path) == (
            tmp_path / ".gemini" / "config" / "hooks.json"
        )

    def test_the_two_roots_are_different_directories(self, tmp_path):
        """The master and a consultation want opposite contents in the
        same file, so sharing one root would have them fighting over it."""
        master = roots.master_root(tmp_path)
        with roots.ephemeral(tmp_path) as consultation:
            assert consultation != master
            assert roots.hooks_file(consultation) != roots.hooks_file(master)

    def test_both_live_under_the_apps_own_config_directory(self, tmp_path):
        """One thing to remove when a user clears state."""
        with roots.ephemeral(tmp_path) as consultation:
            for root in (roots.master_root(tmp_path), consultation):
                assert root.is_relative_to(roots.roots_dir(tmp_path))


class TestTheSeed:
    """228 KB instead of 17 MB, and where the links point."""

    def test_a_prepared_root_links_rather_than_copies(
        self, tmp_path, vendor_install
    ):
        root = roots.prepare(
            tmp_path / "root", tmp_path / "cfg", source=vendor_install
        )
        for name in roots.SEEDED:
            linked = roots.vendor_dir(root) / name
            assert linked.is_symlink()
            assert (linked / f"{name}-payload").read_text() == "x"

    def test_the_links_point_at_our_copy_and_not_at_the_users_tree(
        self, tmp_path, vendor_install
    ):
        """The refutation that survived being measured *wrong*.

        Linking straight into ``~/.gemini/antigravity-cli/bin`` was tried
        and nothing wrote back through it. It is still refused, because
        "did not write this time" is not "cannot write": a vendor upgrade
        re-extracting its helpers would do so *through* the symlink, into
        the one tree the private root exists to stay out of.
        """
        cfg = tmp_path / "cfg"
        root = roots.prepare(tmp_path / "root", cfg, source=vendor_install)
        for name in roots.SEEDED:
            destination = (roots.vendor_dir(root) / name).resolve()
            assert destination.is_relative_to(roots.seed_cache(cfg))
            assert not destination.is_relative_to(vendor_install)

    def test_the_cache_is_populated_once_and_reused(
        self, tmp_path, vendor_install
    ):
        cfg = tmp_path / "cfg"
        roots.ensure_seed(cfg, source=vendor_install)
        marker = roots.seed_cache(cfg) / "bin" / "bin-payload"
        marker.write_text("touched by us", encoding="utf-8")

        roots.prepare(tmp_path / "second", cfg, source=vendor_install)

        assert marker.read_text() == "touched by us", "it re-copied"

    def test_a_machine_with_no_agy_install_still_gets_a_root(self, tmp_path):
        """``None`` from the seed is an answer, not a failure.

        A first run on a machine where ``agy`` has never been used has
        nothing to copy. The root is still usable; it simply pays the 17 MB
        extraction once, and seeds from itself next time.
        """
        cfg = tmp_path / "cfg"
        assert roots.ensure_seed(cfg, source=tmp_path / "nothing-here") is None
        root = roots.prepare(
            tmp_path / "root", cfg, source=tmp_path / "nothing-here"
        )
        assert roots.hooks_file(root).parent.is_dir()
        assert roots.vendor_dir(root).is_dir()

    def test_a_half_copied_cache_is_never_left_behind(
        self, tmp_path, vendor_install, monkeypatch
    ):
        """Staged and renamed, because the failure mode is silent.

        An interrupted ``copytree`` writing straight into the cache would
        leave a directory that looks complete to every later run — which
        would then symlink a partial toolchain into every root and produce
        a vendor that fails in a way nothing here explains.
        """
        import shutil as real_shutil

        def die(*_a, **_k):
            raise OSError("interrupted")

        monkeypatch.setattr(real_shutil, "copytree", die)
        cfg = tmp_path / "cfg"
        assert roots.ensure_seed(cfg, source=vendor_install) is None
        cache = roots.seed_cache(cfg)
        assert not any((cache / name).exists() for name in roots.SEEDED)


class TestAConsultationsRoot:
    """One per consultation, removed in a ``finally``."""

    def test_it_is_gone_afterwards(self, tmp_path):
        with roots.ephemeral(tmp_path) as root:
            assert root.is_dir()
        assert not root.exists()

    def test_it_is_gone_even_when_the_consultation_raised(self, tmp_path):
        with pytest.raises(ValueError):
            with roots.ephemeral(tmp_path) as root:
                raise ValueError("the model said something unhelpful")
        assert not root.exists()

    def test_two_consultations_do_not_share_a_hooks_file(self, tmp_path):
        """The reason there are two roots at all, in one assertion."""
        with roots.ephemeral(tmp_path) as first:
            with roots.ephemeral(tmp_path) as second:
                assert first != second
                roots.hooks_file(first).write_text("{}", encoding="utf-8")
                assert not roots.hooks_file(second).exists()

    def test_a_root_a_dead_process_left_behind_is_swept(self, tmp_path):
        stale = roots.roots_dir(tmp_path) / "consultations" / "c-abandoned"
        stale.mkdir(parents=True)
        (stale / "litter").write_text("x", encoding="utf-8")

        assert roots.sweep_consultations(tmp_path) == 1
        assert not stale.exists()

    def test_it_sweeps_once_and_then_never_again(self, tmp_path, monkeypatch):
        """The latch is safety, not thrift.

        Before this process opens a consultation, everything under that
        directory is somebody else's litter. Afterwards it is not, and a
        second sweep would delete a live consultation's root out from under
        it while the vendor still had files open in it.
        """
        monkeypatch.setattr(roots, "_SWEPT", set())
        roots.sweep_consultations(tmp_path)

        live = roots.roots_dir(tmp_path) / "consultations" / "c-live"
        live.mkdir(parents=True)
        assert roots.sweep_consultations(tmp_path) == 0
        assert live.is_dir()

    def test_sweeping_a_machine_that_has_never_consulted_is_fine(self, tmp_path):
        assert roots.sweep_consultations(tmp_path) == 0


class TestTheEnvironment:
    """An allowlist, and the two entries that are not hygiene."""

    def test_home_is_the_root_and_overrides_the_callers(self, tmp_path):
        env = roots.environment(tmp_path, base={"HOME": "/home/somebody"})
        assert env["HOME"] == str(tmp_path)

    def test_nothing_outside_the_allowlist_survives(self, tmp_path):
        env = roots.environment(
            tmp_path,
            base={
                "PATH": "/usr/bin",
                "ANTHROPIC_API_KEY": "sk-ant-secret",
                "AWS_SECRET_ACCESS_KEY": "also-secret",
                "OPENAI_API_KEY": "third-secret",
            },
        )
        assert env == {"PATH": "/usr/bin", "HOME": str(tmp_path)}

    def test_the_bus_address_is_carried_because_auth_is_on_it(self, tmp_path):
        """Load-bearing, not hygiene.

        Credentials live in the login keyring over the session bus, so
        dropping ``DBUS_SESSION_BUS_ADDRESS`` does not degrade auth — it
        removes it, and the failure arrives as a model that will not answer
        rather than as anything naming an environment variable.
        """
        env = roots.environment(
            tmp_path,
            base={
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
                "XDG_RUNTIME_DIR": "/run/user/1000",
            },
        )
        assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"
        assert env["XDG_RUNTIME_DIR"] == "/run/user/1000"

    def test_an_absent_name_is_omitted_rather_than_set_empty(self, tmp_path):
        """``HTTPS_PROXY=`` is not the same statement as an unset one, and
        some clients read the empty string as a proxy at the empty host."""
        env = roots.environment(tmp_path, base={"PATH": "/usr/bin"})
        assert "HTTPS_PROXY" not in env

    def test_an_empty_value_is_treated_as_absent(self, tmp_path):
        env = roots.environment(
            tmp_path, base={"PATH": "/usr/bin", "NO_PROXY": ""}
        )
        assert "NO_PROXY" not in env

    def test_it_reads_the_process_environment_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TZ", "Australia/Sydney")
        assert roots.environment(tmp_path)["TZ"] == "Australia/Sydney"

    def test_no_credential_of_another_vendors_is_on_the_list(self):
        """The allowlist's whole reason, asserted rather than described.

        ``GEMINI_API_KEY`` and ``GOOGLE_API_KEY`` are there because they are
        *this* vendor's. Anything matching a key-shaped name from somebody
        else would be a credential handed to an agent that has no business
        with it.
        """
        allowed = set(roots.PASSTHROUGH)
        for name in (
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "GITHUB_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
        ):
            assert name not in allowed

    def test_the_allowlist_has_no_duplicates(self):
        assert len(roots.PASSTHROUGH) == len(set(roots.PASSTHROUGH))

    def test_os_environ_is_not_mutated(self, tmp_path):
        before = dict(os.environ)
        roots.environment(tmp_path)
        assert dict(os.environ) == before


class TestItNeverWritesToTheUsersTree:
    """The property the whole decision exists for."""

    def test_preparing_a_root_touches_nothing_in_the_source(
        self, tmp_path, vendor_install
    ):
        def snapshot():
            return sorted(
                (str(p.relative_to(vendor_install)), p.stat().st_mtime_ns)
                for p in vendor_install.rglob("*")
            )

        before = snapshot()
        roots.prepare(tmp_path / "root", tmp_path / "cfg", source=vendor_install)
        with roots.ephemeral(tmp_path / "cfg", source=vendor_install):
            pass
        assert snapshot() == before

    def test_the_user_root_is_the_only_thing_that_names_the_real_home(self):
        """One function reads ``Path.home()``, and it is read-only.

        Anything else doing so would be AG-R-18 again: a value fixed to the
        server's own configuration, used for a process that was spawned
        against a different one.
        """
        source = Path(roots.__file__).read_text(encoding="utf-8")
        assert source.count("Path.home()") == 1
        # In the one place it is allowed to be. Read from the file rather
        # than called, because ``tests/conftest.py`` redirects the live
        # function precisely so that this suite never reads the real tree.
        assert "return Path.home()" in source
