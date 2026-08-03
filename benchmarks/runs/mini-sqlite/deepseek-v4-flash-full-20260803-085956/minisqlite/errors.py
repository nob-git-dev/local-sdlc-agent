"""Shared exception types for MiniSQLite Engine.

Error hierarchy follows SPEC.md section 15.1.
"""


class MiniSQLiteError(Exception):
    """Base class for all MiniSQLite errors."""


class SQLSyntaxError(MiniSQLiteError):
    """Raised when SQL text cannot be parsed."""


class SchemaError(MiniSQLiteError):
    """Raised for missing tables or columns."""


class TypeMismatchError(MiniSQLiteError):
    """Raised when a value type does not match the column type."""


class DuplicateKeyError(MiniSQLiteError):
    """Raised when a rowid already exists."""


class StorageError(MiniSQLiteError):
    """Base class for storage-layer failures."""


class CorruptDatabaseError(StorageError):
    """Raised when a database file is malformed."""
