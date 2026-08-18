"""Tests for the static server's cache headers — the stale-frontend bug.

Field incident, reproduced from a launch in a fresh repo. Every RPC the
webapp made failed with "method not found on proxy": ``LLMService`` had
been deleted in conversion phase 3 and the app in the browser was still
calling it. The backend was current. The tab was not — it had loaded
``index-B68mnaSQ.js``, a hashed bundle that no longer existed anywhere on
disk, together with the ``index.html`` that used to name it. Both came
from the browser's own HTTP cache.

``SimpleHTTPRequestHandler`` sends ``Last-Modified`` and no
``Cache-Control``, which leaves the freshness lifetime to the browser's
heuristic. ``/index.html`` is a stable URL, so that heuristic is free to
reuse the cached copy without revalidating, and a rebuilt ``webapp/dist``
never reaches the page. The symptom presents as a broken engine, which is
why it is worth a test: nothing about "method not found on proxy" points
at a caching header.

The requests go over a real socket to a real server rather than calling
``end_headers`` on a constructed handler, because the thing under test is
what arrives at a client. A handler asserted on in isolation would pass
while the SPA-fallback rewrite, which changes ``self.path`` and so
changes which branch runs, went untested.
"""

from __future__ import annotations

import logging
import os
import socket
import urllib.request
from pathlib import Path

import pytest

from ac_dc import main


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def served(tmp_path: Path) -> str:
    """Serve a minimal dist tree and return its base URL."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text(
        '<script src="./assets/index-abc123.js"></script>',
    )
    (tmp_path / "assets" / "index-abc123.js").write_text("console.log(1)")

    port = _free_port()
    main._start_static_server(tmp_path, port)
    return f"http://127.0.0.1:{port}"


def _headers(url: str) -> dict[str, str]:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return {k.lower(): v for k, v in resp.headers.items()}


def test_index_is_not_stored(served: str) -> None:
    """The entry document names the hashed bundles, so it must never
    be served from cache without asking us first."""
    cc = _headers(f"{served}/index.html")["cache-control"]
    assert "no-store" in cc


def test_spa_fallback_is_not_stored(served: str) -> None:
    """The fallback serves index.html's bytes under some other URL.
    It rewrites ``self.path``, so it is a separate branch and gets
    the same guarantee — this is the path a bare ``/`` takes."""
    cc = _headers(f"{served}/")["cache-control"]
    assert "no-store" in cc


def test_query_string_does_not_defeat_the_rule(served: str) -> None:
    """The URL the browser is actually sent carries ``?port=`` — the
    check must not be fooled into the immutable branch by it."""
    cc = _headers(f"{served}/?port=18080")["cache-control"]
    assert "no-store" in cc


def test_hashed_assets_are_immutable(served: str) -> None:
    """Vite puts a content hash in every asset name, so a changed
    asset is a changed URL and caching it for a year costs nothing.
    Without this the no-store rule would make every reload re-fetch
    ~5 MB of Monaco and KaTeX."""
    cc = _headers(f"{served}/assets/index-abc123.js")["cache-control"]
    assert "immutable" in cc
    assert "no-store" not in cc


# ---------------------------------------------------------------------------
# The same drift, on disk rather than in the cache: a `webapp/dist` that
# `npm run build` last touched before the sources moved. Correct cache
# headers deliver that bundle faithfully and it is still the wrong app.
# ---------------------------------------------------------------------------


def _tree(root: Path, *, src_mtime: float, built_mtime: float) -> Path:
    """A webapp tree with explicitly aged sources and bundle."""
    dist = root / "webapp" / "dist"
    src = root / "webapp" / "src"
    (dist / "assets").mkdir(parents=True)
    src.mkdir(parents=True)

    (dist / "index.html").write_text("<html></html>")
    (src / "app.js").write_text("export const x = 1")

    os.utime(src / "app.js", (src_mtime, src_mtime))
    os.utime(dist / "index.html", (built_mtime, built_mtime))
    return dist


def test_warns_when_sources_are_newer(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    dist = _tree(tmp_path, src_mtime=2_000.0, built_mtime=1_000.0)
    with caplog.at_level(logging.WARNING, logger="ac_dc.main"):
        main._warn_if_dist_is_stale(dist)
    assert "stale webapp" in caplog.text


def test_silent_when_the_build_is_current(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    dist = _tree(tmp_path, src_mtime=1_000.0, built_mtime=2_000.0)
    with caplog.at_level(logging.WARNING, logger="ac_dc.main"):
        main._warn_if_dist_is_stale(dist)
    assert caplog.text == ""


def test_silent_for_an_installed_package(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """``webapp_dist`` ships without sources beside it. Nothing to
    compare is not the same as out of date, and warning every
    installed user would be noise they cannot act on."""
    dist = tmp_path / "ac_dc" / "webapp_dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>")
    with caplog.at_level(logging.WARNING, logger="ac_dc.main"):
        main._warn_if_dist_is_stale(dist)
    assert caplog.text == ""
