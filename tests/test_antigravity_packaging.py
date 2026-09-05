"""Tests for phase 7 — `google-antigravity` as an optional extra.

The wheel ships its own `localharness` binary, measured at 129,065,896
bytes in 0.1.16, on top of the ~295 MB `claude` CLI that is already
mandatory. [AG-R-10](../specs5/plan-ag/risks.md#ag-r-10) is the argument
for making it an extra: an install that grew by that much to carry an
engine the user has no credentials for is a bad trade made silently.

**What makes this phase subtle is that nothing fails without the wheel.**
Every `from google.antigravity import …` in the package is function-local
by design, so a base install imports cleanly, constructs every adapter,
and reports every engine as mountable — right up to the first turn, where
the absence arrives as an `ImportError` from an engine the user picked out
of a selector. That is the "broken UI" the phase's exit criterion forbids,
and it is invisible to a test suite running in an install that *has* the
wheel.

So these tests simulate absence rather than requiring it, and they live in
their own file for a reason worth naming: `test_antigravity_surface.py`
skips its whole module when the SDK is missing, which would skip the
"behaves correctly without the SDK" tests in exactly the install they
describe.

Offline. Nothing here imports the SDK, starts a harness or touches a
network.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

from aic_dc.antigravity import consultant as consultant_module
from aic_dc.antigravity import surface
from aic_dc.antigravity.consultant import Consultant
from aic_dc.antigravity.credentials import GEMINI_API, NONE, Credentials

REPO = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))


class TestItIsDeclaredAsAnExtra:
    def test_it_is_not_a_base_dependency(self):
        """AG-R-10's tripwire, as an assertion rather than a release ritual.

        *"Base-install size, measured per release. A jump means the extra
        has leaked into the default dependency set — which is a
        pyproject.toml edit nobody reviews as a size change."* A test is
        the cheaper half of that: the edit fails here before anyone has to
        notice 136 MiB.
        """
        base = _pyproject()["project"]["dependencies"]
        assert not any("google-antigravity" in d for d in base), (
            "google-antigravity is back in the base dependency set. That is "
            "136 MiB on every install, for an engine the agy transport "
            "already reaches without it (AG-R-10)."
        )

    def test_the_extra_exists_and_names_the_package(self):
        extras = _pyproject()["project"]["optional-dependencies"]
        assert "antigravity" in extras
        assert any("google-antigravity" in d for d in extras["antigravity"])

    def test_the_floor_is_pinned(self):
        """0.1.x and alpha (AG-R-2), so a floor is the least we can do.

        Unpinned was defensible while nobody had read the package; the
        surface has now been read twice and there is a drift gate over it.
        """
        (spec,) = _pyproject()["project"]["optional-dependencies"]["antigravity"]
        assert ">=" in spec, f"{spec!r} names no minimum version"


class TestSdkInstalledIsTheOneAuthority:
    def test_it_is_false_when_the_namespace_package_is_absent(self, monkeypatch):
        """The real shape of a base install, measured in one.

        `find_spec('google.antigravity')` **raises** `ModuleNotFoundError`
        when the `google` namespace package is not there at all, rather
        than returning None — and that is precisely the state a base
        install is in. Unguarded it is an uncaught exception at startup,
        which is a worse failure than the one this whole phase is about.
        """

        def explode(name):
            raise ModuleNotFoundError("No module named 'google'")

        monkeypatch.setattr(surface.importlib.util, "find_spec", explode)
        assert surface.sdk_installed() is False

    def test_it_is_false_when_only_the_submodule_is_absent(self, monkeypatch):
        monkeypatch.setattr(surface.importlib.util, "find_spec", lambda name: None)
        assert surface.sdk_installed() is False

    def test_a_parent_without_a_spec_is_not_installed(self, monkeypatch):
        """`find_spec` raises ValueError for a parent with no `__spec__`."""

        def explode(name):
            raise ValueError("__spec__ is not set")

        monkeypatch.setattr(surface.importlib.util, "find_spec", explode)
        assert surface.sdk_installed() is False

    def test_the_probe_asks_it_rather_than_deciding_for_itself(self, monkeypatch):
        """One answer to "is there an SDK", not two that can disagree."""
        monkeypatch.setattr(surface, "sdk_installed", lambda: False)
        assert surface._sdk() is None

    def test_it_does_not_import_the_sdk_to_answer(self, monkeypatch):
        """It runs at every startup, including runs that never touch this
        engine. Importing pydantic and gRPC to answer a yes/no question
        would put that cost on a path meant to be free."""
        called = []
        monkeypatch.setattr(surface, "_sdk", lambda: called.append(1))
        surface.sdk_installed()
        assert not called


class TestABaseInstallOffersNothingBroken:
    """The exit criterion, stated as the things that would break it.

    The consultant checks are patched on ``consultant_module`` rather than
    on ``surface``: the module does ``from …surface import sdk_installed``,
    so its name is bound at import time and patching the definition's home
    would leave the caller looking at the original. Stated here rather
    than discovered again — the patch that silently does nothing is the
    one that makes a test pass for the wrong reason.
    """

    def test_the_consultant_is_absent_rather_than_broken(self, monkeypatch):
        """A key without a wheel used to register two tools that raised.

        `available` was credentials-only, so a base install with a Gemini
        key mounted `second_opinion` and `generate_image`, spent context
        describing them on every turn, and answered the first call with an
        ImportError. AG-9's "hidden rather than stubbed", applied to a
        tool definition.
        """
        monkeypatch.setattr(consultant_module, "sdk_installed", lambda: False)
        keyed = Credentials(mode=GEMINI_API, api_key="k", source="test")
        assert keyed.available is True
        assert Consultant("/tmp", credentials=keyed).available is False

    def test_it_is_available_with_both(self, monkeypatch):
        monkeypatch.setattr(consultant_module, "sdk_installed", lambda: True)
        keyed = Credentials(mode=GEMINI_API, api_key="k", source="test")
        assert Consultant("/tmp", credentials=keyed).available is True

    def test_a_wheel_without_a_credential_is_still_unavailable(self, monkeypatch):
        """AG-R-8 has not been repealed; this adds a condition to it."""
        monkeypatch.setattr(consultant_module, "sdk_installed", lambda: True)
        none = Credentials(mode=NONE, source="nowhere")
        assert Consultant("/tmp", credentials=none).available is False

    def test_the_mount_is_gated_on_the_wheel_and_not_only_the_key(self):
        """Read off `main.py`'s own syntax, because the alternative is
        starting a server in a test.

        The failure this guards is a one-word edit: dropping the SDK
        condition leaves an engine in the selector that raises on its
        first turn, and every offline test stays green because the test
        environment has the wheel.
        """
        source = (REPO / "src" / "aic_dc" / "main.py").read_text(encoding="utf-8")
        assert "antigravity_sdk = sdk_installed()" in source
        assert "antigravity_credentials.available and antigravity_sdk" in source

    def test_the_agy_transport_is_not_gated_on_the_wheel(self):
        """What makes the extra affordable: the same product, no wheel.

        `agy` drives the Antigravity CLI over a pipe and imports nothing
        from `google.antigravity`, so a base install still reaches this
        engine — on the owner's subscription rather than on a metered key.
        Gating it on the SDK would turn the extra from "the API-key route
        is optional" into "Antigravity is optional", which is a different
        and much worse trade.
        """
        agy = REPO / "src" / "aic_dc" / "agy"
        for path in sorted(agy.rglob("*.py")):
            assert "google.antigravity" not in path.read_text(encoding="utf-8"), (
                f"{path.name} reaches for the SDK. The agy transport is the "
                "one that survives a base install; it must stay that way."
            )


def test_nothing_imports_the_sdk_at_module_scope():
    """What makes a base install importable rather than broken.

    Every `from google.antigravity import …` in this package is
    function-local by design. A single top-level one would turn a base
    install from "one engine plus the agy transport" into an ImportError
    at startup — and would do it in whichever module happened to be
    imported first, which is nobody's idea of a diagnostic.
    """
    package = REPO / "src" / "aic_dc"
    offenders = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:  # module scope only
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            if any(n.startswith("google.antigravity") for n in names):
                offenders.append(str(path.relative_to(package)))
    assert not offenders, (
        f"{offenders} import the SDK at module scope. It is an optional extra "
        "(AG-R-10), so that makes a base install fail at startup."
    )
