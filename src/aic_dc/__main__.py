"""Entry point for ``python -m aic_dc``, and the PyInstaller entry script.

Delegates to the argparse-based CLI in :mod:`aic_dc.cli`.

``SystemExit(main())`` rather than a bare ``main()``: this module is what
the release binary is built from, so discarding the return value makes
every exit code the CLI computes unobservable from a shell. That was
silent while every path returned 0 and stopped being silent with
``--check-engine``, whose whole output is its exit code — a CI step
asserting on it would have passed unconditionally.
"""

from aic_dc.cli import main

if __name__ == "__main__":
    raise SystemExit(main())