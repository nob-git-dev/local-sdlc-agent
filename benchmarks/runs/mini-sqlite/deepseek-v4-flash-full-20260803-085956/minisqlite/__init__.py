"""MiniSQLite public API.

Re-exports the user-facing Connection, connect, and Result symbols so
callers can import them directly from the package root.
"""

from minisqlite.connection import Connection, connect
from minisqlite.result import Result

__all__ = ["Connection", "connect", "Result"]