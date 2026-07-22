"""
MiniSQLite Engine

A minimal SQLite-like database engine implementation in Python.
"""

from minisqlite.connection import Connection, connect
from minisqlite.result import Result
from minisqlite.errors import (
    MiniSQLiteError,
    SQLSyntaxError,
    SchemaError,
    TypeMismatchError,
    DuplicateKeyError,
    StorageError,
    CorruptDatabaseError,
)

__all__ = [
    "Connection",
    "connect",
    "Result",
    "MiniSQLiteError",
    "SQLSyntaxError",
    "SchemaError",
    "TypeMismatchError",
    "DuplicateKeyError",
    "StorageError",
    "CorruptDatabaseError",
]