"""Entry point for ``python -m aic_dc``.

Delegates to the argparse-based CLI in :mod:`aic_dc.cli`.
"""

from aic_dc.cli import main

if __name__ == "__main__":
    main()