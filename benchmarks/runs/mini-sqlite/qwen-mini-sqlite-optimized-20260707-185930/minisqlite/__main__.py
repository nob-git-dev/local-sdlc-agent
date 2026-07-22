"""
CLI entry point for MiniSQLite Engine.

Usage:
    python -m minisqlite <database_path> [sql_statement]
    python -m minisqlite <database_path>  # Interactive mode
"""

import sys

from minisqlite.cli import main


if __name__ == "__main__":
    sys.exit(main())