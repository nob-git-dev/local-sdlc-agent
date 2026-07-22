"""
MiniSQLite Error Definitions

Defines the exception hierarchy for the MiniSQLite engine.
"""


class MiniSQLiteError(Exception):
    """Base exception for all MiniSQLite errors."""
    pass


class SQLSyntaxError(MiniSQLiteError):
    """Raised when SQL syntax is invalid."""
    pass


class SchemaError(MiniSQLiteError):
    """Raised for schema-related errors (e.g., table not found, column not found)."""
    pass


class TypeMismatchError(MiniSQLiteError):
    """Raised when a value type does not match the expected column type."""
    pass


class DuplicateKeyError(MiniSQLiteError):
    """Raised when inserting a duplicate rowid."""
    pass


class StorageError(MiniSQLiteError):
    """Raised for storage-related errors (e.g., I/O errors)."""
    pass


class CorruptDatabaseError(StorageError):
    """Raised when the database file is corrupted (e.g., invalid magic bytes, page type)."""
    pass
