"""Entry point for the MiniSQLite Engine CLI.

Running ``python -m minisqlite`` delegates to the CLI implementation in
:mod:`minisqlite.cli` and exits with the CLI's return code.
"""

import sys

from minisqlite.cli import main


if __name__ == "__main__":
    sys.exit(main())