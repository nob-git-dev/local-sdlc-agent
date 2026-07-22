"""
Connection API for MiniSQLite

Provides the main user-facing API for database operations.
"""

import os
from typing import Optional

from minisqlite.errors import MiniSQLiteError
from minisqlite.result import Result
from minisqlite.sql.lexer import tokenize
from minisqlite.sql.parser import Parser
from minisqlite.engine.schema import Schema
from minisqlite.engine.executor import Executor
from minisqlite.storage.pager import Pager


class Connection:
    """
    Database connection object.

    Provides the main API for executing SQL statements.
    """

    def __init__(self, db_path: str):
        """
        Create a connection to a database.

        Args:
            db_path: Path to the database file
        """
        self.db_path = db_path
        self._pager = Pager(db_path)
        self._schema = Schema(self._pager)
        self._executor = Executor(self._schema)
        self._closed = False

    def execute(self, sql: str) -> Result:
        """
        Execute a SQL statement.

        Args:
            sql: SQL statement to execute

        Returns:
            Result object containing columns, rows, and rowcount

        Raises:
            MiniSQLiteError: On any error during execution
        """
        if self._closed:
            raise MiniSQLiteError("Connection is closed")

        # Tokenize
        tokens = tokenize(sql)

        # Parse
        parser = Parser(tokens)
        statement = parser.parse()

        # Execute
        return self._executor.execute(statement)

    def close(self) -> None:
        """Close the connection and flush any pending writes."""
        if not self._closed:
            self._pager.flush()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


def connect(db_path: str) -> Connection:
    """
    Create a new database connection.

    Args:
        db_path: Path to the database file

    Returns:
        Connection object
    """
    return Connection(db_path)