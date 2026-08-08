"""Connection API for MiniSQLite Engine.

This module implements the user-facing Connection class described in
SPEC.md section 4.3.  A Connection owns a Pager, a SchemaRegistry, and
an Executor.  The module-level connect() function opens a database file
and returns a Connection.
"""

from minisqlite.engine.executor import Executor
from minisqlite.engine.schema import SchemaRegistry
from minisqlite.storage.pager import Pager


class Connection:
    """A database connection that owns the storage and schema layers.

    Connection(path) opens the database file at path, constructs the
    Pager, SchemaRegistry, and Executor, and exposes execute(sql) and
    close().  It also supports the context manager protocol.
    """

    def __init__(self, path):
        self._pager = Pager(path)
        self._schema = SchemaRegistry(self._pager)
        self._executor = Executor(self._schema, self._pager)
        self._closed = False

    def execute(self, sql):
        """Parse and execute a single SQL statement.

        Raises an error if the connection is closed or sql is not a
        string.  Returns the Result produced by the Executor.
        """
        if self._closed:
            raise RuntimeError("connection is closed")
        if not isinstance(sql, str):
            raise TypeError("sql must be a string")
        from minisqlite.sql.parser import parse

        statement = parse(sql)
        return self._executor.execute(statement, sql)

    def close(self):
        """Flush and close the underlying Pager.

        This method is idempotent: calling it more than once is safe.
        """
        if self._closed:
            return
        self._pager.flush()
        self._pager.close()
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False


def connect(path):
    """Open a database file and return a Connection.

    The returned Connection must be closed when no longer needed.
    """
    return Connection(path)