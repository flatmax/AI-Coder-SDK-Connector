"""Hatchling build hook — ship the built webapp in the wheel when it exists.

A pip-installed release has to be able to serve the webapp without the user
running ``npm run build`` first, and ``aic_dc.main._find_webapp_dist`` already
looks for it as installed package data at ``<package>/webapp_dist``. Nothing
was putting it there, so the third entry in that lookup's priority list had no
producer and pip installs fell through to the GitHub Pages fallback
(specs5/6-deployment/build.md § Webapp Location Priority).

**Why a hook rather than ``force-include``.** The declarative form,

    [tool.hatch.build.targets.wheel.force-include]
    "webapp/dist" = "aic_dc/webapp_dist"

fails the build when the source path is missing, and in a dev checkout it is
missing until someone runs a Vite build. That turns ``uv sync`` — the first
command in the contributing path — into an error whose fix is to build a
frontend you may not be working on. A hook can ask.

So the rule is: include it when it is there, stay quiet when it is not. The
release workflow builds the webapp before building the wheel, so the artefact
that reaches users has it; a dev checkout gets a backend-only wheel, which is
what ``--dev`` and ``--preview`` want anyway since they run Vite themselves.

The failure mode this leaves is a release built without the npm step, which
ships a wheel that silently cannot serve a webapp. That is why the workflow
asserts on the built wheel rather than trusting the ordering of its own steps.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

#: Where the wheel carries the webapp, and where
#: ``aic_dc.main._find_webapp_dist`` looks for it. Changing one without the
#: other produces an install that serves nothing, so they are named together
#: here and in that function's docstring.
WHEEL_DEST = "aic_dc/webapp_dist"


class WebappBuildHook(BuildHookInterface):
    """Force-include ``webapp/dist`` at :data:`WHEEL_DEST` when it is built."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        """Add the webapp to the wheel's file map if a build is present.

        Tests for ``index.html`` rather than the directory: an empty or
        half-cleaned ``webapp/dist`` would otherwise produce a wheel that
        claims to carry a webapp and serves a 404, which is harder to
        diagnose than not carrying one at all.
        """
        dist = Path(self.root) / "webapp" / "dist"
        if not (dist / "index.html").is_file():
            self.app.display_info(
                f"webapp: no build at {dist}/index.html — wheel will be "
                f"backend only (run `npm run build` in webapp/ to include it)"
            )
            return
        build_data["force_include"][str(dist)] = WHEEL_DEST
        self.app.display_info(f"webapp: including {dist} as {WHEEL_DEST}")
