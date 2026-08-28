"""Every RPC has a caller, or is listed here with the reason it has none.

``add_service`` publishes every public method on the five registered
instances, so the RPC surface is not a list anybody wrote — it is whatever
those classes happen to expose. Two things go wrong because of that, and
neither one breaks a build:

- **A method written for one Python caller becomes an RPC.** Nothing warns.
  The docstring goes on describing an internal helper while the browser can
  call it; ``Settings.is_reloadable`` even argues the point against itself
  mid-sentence and leaves the conclusion in prose.
- **An RPC loses its last caller and stays.** ``reconnect_mcp_server`` and
  ``toggle_mcp_server`` sat callerless and unnoticed until the Context tab
  was written. ``specs5/impl-history/work-log.md`` § *How to keep this from
  recurring* asked for this test on the strength of that pair, and guessed
  there were others; the audit behind the tables below found twelve. One of
  the twelve, ``set_viewer_state``, has since been given the caller it was
  waiting for and is gone from :data:`DORMANT`.

So the surface is partitioned three ways and the partition is asserted:
called from the browser (derived by scanning ``webapp/src``, never listed
here), :data:`INTERNAL_ONLY` (a Python caller exists, and the method is
public only because jrpc-oo publishes every public method), and
:data:`DORMANT` (nothing calls it anywhere). A new public method belongs to
none of them, which fails
:func:`test_every_rpc_has_a_caller_or_is_listed_as_dormant` and asks the
question the classification exists to force: is this meant to be reachable
from a browser?

The scan is the load-bearing part, so its rule is deliberately narrow — a
fully-qualified name inside quotes, on a line that is not a comment. Both
calling conventions spell the name exactly that way
(``call['Repo.get_file_tree']`` and
``this.rpcExtract('Repo.get_file_tree')``), and the one indirection,
``rpc-mixin.js``'s ``call[method]``, takes the name from its caller — so
there is no dynamic construction to miss. The narrowness was checked
rather than assumed: against a looser scan that keeps comment lines, the
two agree exactly, so no method is being credited with a caller that is
really a mention in prose.

This is ``specs5/next.md`` § C5. The audit was the task and this file is
its by-product, which is why the tables carry reasons rather than bare
names: the entry is the finding.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import aic_dc
from aic_dc.claude_code import ClaudeCodeService
from aic_dc.collab import Collab
from aic_dc.doc_convert.service import DocConvert
from aic_dc.repo import Repo
from aic_dc.settings import Settings

# The five instances ``main.py`` registers, under the namespace jrpc-oo
# derives from the class name. Kept as classes rather than instances
# because ``dir()`` answers the same and construction needs a repo.
REGISTERED = (Collab, Repo, Settings, DocConvert, ClaudeCodeService)


# ---------------------------------------------------------------------------
# Public only because jrpc-oo publishes every public method
# ---------------------------------------------------------------------------
#
# Each value names a file that calls the method from Python. These are not
# defects — the code is reached and works — but each one is an RPC nobody
# designed, so the browser can call it and the docstring will not say what
# happens when it does. The value is checked, not decorative: a method whose
# named caller stops calling it is either dormant or moved, and both are
# worth knowing.

INTERNAL_ONLY: dict[str, str] = {
    # Collaboration's own bookkeeping, read by the services that gate on it.
    "Collab.get_connected_clients": "src/aic_dc/claude_code/service.py",
    "Collab.is_caller_localhost": "src/aic_dc/claude_code/service.py",
    # Review mode's git arrangement. ``ReviewMode`` drives the whole
    # sequence; the browser calls ``start_review`` / ``end_review`` and
    # never the steps.
    "Repo.checkout_review_parent": "src/aic_dc/claude_code/review.py",
    "Repo.exit_review_mode": "src/aic_dc/claude_code/review.py",
    "Repo.get_commit_log": "src/aic_dc/claude_code/review.py",
    "Repo.get_commit_parent": "src/aic_dc/repo/review.py",
    "Repo.get_merge_base": "src/aic_dc/repo/review.py",
    "Repo.get_review_changed_files": "src/aic_dc/claude_code/review.py",
    "Repo.resolve_ref": "src/aic_dc/repo/review.py",
    "Repo.setup_review_soft_reset": "src/aic_dc/claude_code/review.py",
    # A ``ClaudeCodeService`` twin delegates here through ``ReviewMode``.
    # The pair is the reason the service twins look callerless below — they
    # are not the same method.
    "Repo.get_commit_graph": "src/aic_dc/claude_code/review.py",
    # Reached the same way, but by a caller that is itself dormant: the
    # chain from ``ClaudeCodeService.get_review_file_diff`` down to here has
    # no browser end, so this is transitively dead rather than internal.
    # Listed on the evidence — it does have a Python caller — and the
    # reason it has no browser one is under that entry in DORMANT.
    "Repo.get_review_file_diff": "src/aic_dc/claude_code/review.py",
    # The agent's commit path. The browser's commit button goes to
    # ``ClaudeCodeService.commit_all``, which composes these.
    "Repo.commit": "src/aic_dc/claude_code/commit.py",
    "Repo.reset_hard": "src/aic_dc/claude_code/commit.py",
    "Repo.stage_all": "src/aic_dc/claude_code/commit.py",
    # Tool probes for the TeX preview, asked before shelling out.
    "Repo.is_make4ht_available": "src/aic_dc/repo/tex_preview.py",
    "Repo.is_tex4ht_package_available": "src/aic_dc/repo/tex_preview.py",
    # Feeds the graph's branch decoration.
    "Repo.list_branches": "src/aic_dc/repo/commit_graph.py",
    # The walk every index starts from.
    "Repo.get_flat_file_list": "src/aic_dc/claude_code/review.py",
    # Lazy connect: the first turn calls it, so the browser never has to.
    "ClaudeCodeService.connect_engine": "src/aic_dc/claude_code/service.py",
    # The state snapshot carries both, so the browser reads them from
    # ``get_current_state`` and never asks for either on its own.
    "ClaudeCodeService.get_denied_read_files": "src/aic_dc/claude_code/service.py",
    "ClaudeCodeService.get_review_state": "src/aic_dc/claude_code/service.py",
    # Graceful teardown, from the exit path's bounded window on the loop.
    # Dormant until next.md § C8 decided it, with a docstring that had
    # reasoned about this caller for its whole life.
    "ClaudeCodeService.shutdown": "src/aic_dc/main.py",
}


# ---------------------------------------------------------------------------
# Nothing calls these, anywhere
# ---------------------------------------------------------------------------
#
# The value is the reason, and it has to be one of three things: a decision
# in ``specs5/next.md`` § E, a queued item, or an admission that the method
# is unused. "Unused" is a legitimate entry — the point of the list is that
# the absence is written down instead of being rediscovered as a bug.
#
# **What is asserted about this list is narrower than what it claims.** The
# browser direction is checked both ways; the Python direction is only
# checked for :data:`INTERNAL_ONLY`, where an entry names a file and the
# call in it is verified. So a dormant method that *gains* a Python caller
# keeps a stale entry until somebody moves it — which is what happened to
# ``shutdown`` (next.md § C8), and it moved by hand. A repo-wide scan for
# ``.method(`` would catch that case and is deliberately not here: it
# over-matches on names the tree shares, and ``shutdown`` is the example —
# ``self._executor.shutdown(wait=False)`` is not this method.

DORMANT: dict[str, str] = {
    # § E — the collaboration admission UI is on pause. The browser
    # receives the pushes and re-dispatches them as window events nothing
    # listens to, so no click ever reaches these and every request after
    # the first connection is auto-denied by the 120-second timeout.
    "Collab.admit_client": "admission UI on pause (next.md § E)",
    "Collab.deny_client": "admission UI on pause (next.md § E)",
    # § E — CC-20. ``rewind_files`` refuses the call and names git,
    # because the engine keeps no checkpoints in a session that mirrors
    # its transcript and every session with a repo mirrors. The service
    # method exists so the refusal has somewhere to come from.
    "ClaudeCodeService.rewind_files": "undo is not buildable (next.md § E, CC-20)",
    # § E — collaboration's file-navigation sync. The browser navigates
    # through its own ``navigate-file`` window event and never broadcasts,
    # so "when *any* client navigates" is one client. Dormant for the same
    # reason as the admission pair above: with one client there is nobody
    # to sync to. ``set_viewer_state`` was the other half of next.md § C7
    # and now has a caller (``app-shell/viewer-framing.js``); this half
    # waits on the pause.
    "ClaudeCodeService.navigate_file": "navigation is never broadcast (next.md § E)",
    # A second answer to a question review mode's own git arrangement
    # already answers. The soft reset puts HEAD at the merge-base, so every
    # review change is a staged modification and the diff viewer's ordinary
    # ``get_file_content(path, 'HEAD')`` versus working-tree pair *is* the
    # review diff (``4-features/code-review.md`` § Git State Machine —
    # Entry Sequence, step 6). A unified diff string has no consumer at
    # all: the viewer hands two file contents to Monaco.
    # Same shape as next.md § C3 — two mechanisms, one of them unused.
    "ClaudeCodeService.get_review_file_diff": "the soft reset makes it unnecessary",
    # Unused, and harmless. A pure query over a module-level constant,
    # whose own docstring works out mid-sentence that it is exposed:
    # "Underscore-prefixed so it's not auto-exposed … actually wait,
    # jrpc-oo exposes everything non-underscored."
    "Settings.is_reloadable": "unused pure query; see its docstring",
    # A thin wrapper over the module-level function of the same name,
    # which is what every caller uses. Only its own test calls the method.
    "DocConvert.parse_provenance_body": "wrapper; callers use the module function",
    # Unused. Each has a client-side or git-side equivalent that won.
    "Repo.file_exists": "unused",
    "Repo.is_binary_file": "unused",
    "Repo.search_commits": "unused",
}


# ---------------------------------------------------------------------------
# Called by the browser, exposed by nobody
# ---------------------------------------------------------------------------
#
# The audit run backwards. A call to a namespace the server does not
# register cannot work, and jrpc-oo reports it as a transport error rather
# than a missing method, so it reads as a connection fault.

UNEXPOSED: dict[str, str] = {
    # § E — CC-12. The code/doc mode toggle stays mounted and inert:
    # removing the receiver while leaving its consumer mounted moves the
    # break instead of fixing it. Both call sites are guarded and neither
    # is reachable. ``LLMService`` and ``src/aic_dc/llm/`` went in phase 3.
    "LLMService.switch_mode": "preset selector left inert (next.md § E, CC-12)",
}


# The namespaces worth scanning for. ``AcApp`` is the server-to-browser
# push direction and is included so a call in the wrong direction shows up
# as unexposed rather than as nothing.
NAMESPACES = (
    "Collab",
    "Repo",
    "Settings",
    "DocConvert",
    "ClaudeCodeService",
    "LLMService",
    "AcApp",
)

_NS_ALTERNATION = "|".join(NAMESPACES)
_QUALIFIED = re.compile(
    rf"""['"]((?:{_NS_ALTERNATION})\.[A-Za-z_][A-Za-z_0-9]*)['"]"""
)


def _project_root() -> Path:
    return Path(aic_dc.__file__).resolve().parent.parent.parent


def _exposed() -> set[str]:
    """The RPC surface, by the rule ``ExposeClass`` applies."""
    return {
        f"{cls.__name__}.{name}"
        for cls in REGISTERED
        for name in dir(cls)
        if not name.startswith("_") and callable(getattr(cls, name, None))
    }


def _browser_calls() -> dict[str, list[str]]:
    """Fully-qualified RPC names called from ``webapp/src``, with locations.

    Test files are excluded because a test double is not a caller: mocking
    ``'ClaudeCodeService.get_context_usage'`` proves a test exists, not
    that anything in the app asks for it. Comment lines are dropped for the
    same reason — a docstring naming an RPC is documentation.
    """
    root = _project_root() / "webapp" / "src"
    found: dict[str, list[str]] = {}
    if not root.is_dir():
        return found
    for path in sorted(root.rglob("*.js")):
        if ".test." in path.name:
            continue
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if line.lstrip().startswith(("//", "*", "/*")):
                continue
            for name in _QUALIFIED.findall(line):
                found.setdefault(name, []).append(
                    f"{path.relative_to(root)}:{lineno}"
                )
    return found


def _webapp_present() -> bool:
    """An installed-package run has no ``webapp/src`` to scan."""
    return (_project_root() / "webapp" / "src").is_dir()


class TestRpcSurface:
    """Who calls what, across every registered service."""

    def test_every_rpc_has_a_caller_or_is_listed_as_dormant(self):
        """The whole surface is accounted for, three ways.

        This is the assertion the work-log asked for. It fails on the next
        public method added to any registered service, and on the next one
        whose last caller goes away, and in both cases the fix is to decide
        which list it belongs in — which is the decision that would
        otherwise not get made.
        """
        if not _webapp_present():
            return
        listed = set(INTERNAL_ONLY) | set(DORMANT)
        assert not (set(INTERNAL_ONLY) & set(DORMANT)), (
            "a method cannot be both internal and dormant"
        )
        from_browser = set(_browser_calls()) & _exposed()
        unaccounted = _exposed() - from_browser - listed
        assert unaccounted == set(), (
            "these RPCs have no browser caller and are in neither table — "
            "add each to INTERNAL_ONLY with the file that calls it, or to "
            f"DORMANT with the reason nothing does: {sorted(unaccounted)}"
        )

    def test_the_tables_name_only_real_methods(self):
        """A renamed or deleted method must not leave a live-looking entry.

        The failure without this: a table that describes a surface the code
        no longer has, which is the same silence the tables exist to break.
        """
        stale = (set(INTERNAL_ONLY) | set(DORMANT)) - _exposed()
        assert stale == set(), f"listed but no longer exposed: {sorted(stale)}"

    def test_a_listed_method_is_not_called_from_the_browser(self):
        """Rot in the other direction: a caller arrives, the entry stays.

        This one has already fired for real. Wiring ``set_viewer_state``
        (next.md § C7) failed here, naming the file and line of the new
        caller, which is how the entry got removed in the same commit
        rather than left asserting a gap it had just closed.
        """
        if not _webapp_present():
            return
        calls = _browser_calls()
        wrong = {
            name: calls[name]
            for name in sorted(set(INTERNAL_ONLY) | set(DORMANT))
            if name in calls
        }
        assert wrong == {}, (
            f"listed as having no browser caller, but called from: {wrong}"
        )

    @pytest.mark.parametrize("name", sorted(INTERNAL_ONLY))
    def test_each_internal_only_method_is_called_where_it_says(self, name):
        """The named file really does call it.

        Checked rather than trusted, because the value is the only evidence
        that an entry belongs in this table and not the other one. Matching
        on ``.method(`` finds the call in the file whether the receiver is
        ``self``, ``self._repo`` or a local.
        """
        root = _project_root()
        source = root / INTERNAL_ONLY[name]
        if not source.is_file():
            return
        method = name.split(".", 1)[1]
        calls = [
            line
            for line in source.read_text(encoding="utf-8").splitlines()
            if f".{method}(" in line and f"def {method}" not in line
        ]
        assert calls, (
            f"{INTERNAL_ONLY[name]} no longer calls {method} — {name} is "
            "either dormant now or called from somewhere else"
        )

    def test_the_browser_calls_nothing_the_server_does_not_expose(self):
        """The audit backwards, and it has one standing answer.

        A call into an unregistered namespace fails as a transport error
        rather than a missing method, so it reads as a dropped connection.
        CC-12's inert pair is listed; a second one would be a new defect.
        """
        if not _webapp_present():
            return
        calls = _browser_calls()
        unexposed = {
            name: locations
            for name, locations in sorted(calls.items())
            if name not in _exposed() and name not in UNEXPOSED
        }
        assert unexposed == {}, (
            "the webapp calls RPCs no registered service exposes: "
            f"{unexposed}"
        )

    @pytest.mark.parametrize("name", sorted(UNEXPOSED))
    def test_each_unexposed_call_is_still_there_to_excuse(self, name):
        """Delete the call, delete the entry.

        Without this, :data:`UNEXPOSED` would quietly outlive the code it
        excuses and go on excusing a call somebody might make again.
        """
        if not _webapp_present():
            return
        assert name in _browser_calls(), (
            f"{name} is no longer called from webapp/src — remove it from "
            "UNEXPOSED"
        )

    def test_the_scan_is_not_crediting_comments_as_callers(self):
        """The narrow rule and a loose one agree, so narrowness costs nothing.

        If they ever disagree, a method is being called on a line this scan
        skips — and the tables built on it are wrong in the direction that
        hides a dormant RPC.
        """
        if not _webapp_present():
            return
        root = _project_root() / "webapp" / "src"
        loose: set[str] = set()
        for path in root.rglob("*.js"):
            if ".test." in path.name:
                continue
            loose.update(_QUALIFIED.findall(path.read_text(encoding="utf-8")))
        assert loose == set(_browser_calls()), (
            "quoted RPC names appear only on comment lines somewhere: "
            f"{sorted(loose - set(_browser_calls()))}"
        )
