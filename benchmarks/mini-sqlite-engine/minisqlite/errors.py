"""Mini SQLite Engine error types."""


class MiniSQLiteError(Exception):
    """Base error for Mini SQLite Engine."""


class SQLSyntaxError(MiniSQLiteError):
    """Raised when SQL syntax is invalid."""


class SchemaError(MiniSQLiteError):
    """Raised when a schema definition is invalid."""


class TypeMismatchError(MiniSQLiteError):
    """Raised when a type mismatch occurs."""


class DuplicateKeyError(MiniSQLiteError):
    """Raised when a duplicate key constraint is violated."""


class StorageError(MiniSQLiteError):
    """Raised when a storage operation fails."""


class CorruptDatabaseError(MiniSQLiteError):
    """Raised when the database file appears corrupt."""
