"""Tests for the connection layer (Stage 3A and 3B)."""

import json
import os
import shutil
import tempfile
import unittest

from minisqlite import connect, Result
from minisqlite.errors import (
    DuplicateKeyError,
    SchemaError,
    TypeMismatchError,
)


class TestConnection(unittest.TestCase):
    """Test cases for connect() and CREATE TABLE."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _db_path(self, name: str = "test.db") -> str:
        return os.path.join(self._tmpdir, name)

    def test_connect_is_importable(self):
        """connect is importable from minisqlite."""
        from minisqlite import connect as c
        self.assertTrue(callable(c))

    def test_create_table_returns_empty_result(self):
        """CREATE TABLE returns an empty Result."""
        path = self._db_path()
        conn = connect(path)
        result = conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
        )
        self.assertIsInstance(result, Result)
        self.assertEqual(result.columns, [])
        self.assertEqual(result.rows, [])
        self.assertEqual(result.rows_affected, 0)
        conn.close()

    def test_create_table_duplicate_raises_schema_error(self):
        """Creating the same table twice raises SchemaError."""
        path = self._db_path()
        conn = connect(path)
        conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
        )
        with self.assertRaises(SchemaError):
            conn.execute(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
            )
        conn.close()

    def test_reopen_preserves_table_metadata(self):
        """Reopening the database preserves the table metadata.

        A second CREATE TABLE with the same name after reopen must raise
        SchemaError.
        """
        path = self._db_path()
        conn = connect(path)
        conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
        )
        conn.close()

        # Reopen
        conn2 = connect(path)
        with self.assertRaises(SchemaError):
            conn2.execute(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
            )
        conn2.close()

    def test_table_root_page_initialized(self):
        """The persisted root_page_id can be loaded by BTree(pager, root_page_id=...)
        without CorruptDatabaseError.
        """
        from minisqlite.storage.btree import BTree
        from minisqlite.storage.pager import Pager

        path = self._db_path()
        conn = connect(path)
        conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
        )

        # Read the schema directly to get the root_page_id
        pager = Pager(path)
        raw = pager.read_page(1)
        text = raw.rstrip(b"\x00").decode("utf-8")
        schema = json.loads(text)
        root_page_id = schema["tables"]["users"]["root_page_id"]

        # Verify BTree can load this page without error
        btree = BTree(pager, root_page_id=root_page_id)
        self.assertEqual(btree.root_page_id, root_page_id)

        pager.close()
        conn.close()


class TestInsertSelectDelete(unittest.TestCase):
    """Test cases for INSERT, SELECT, DELETE (Stage 3B)."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _db_path(self, name: str = "test.db") -> str:
        return os.path.join(self._tmpdir, name)

    def test_insert_one_row_and_select_all(self):
        """Insert one row and SELECT * returns the expected columns and row."""
        path = self._db_path()
        conn = connect(path)
        conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
        )
        conn.execute(
            "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);"
        )
        result = conn.execute("SELECT * FROM users;")
        self.assertEqual(result.columns, ["id", "name", "age"])
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0], [1, "Alice", 30])
        conn.close()

    def test_insert_multiple_rows_and_select_all(self):
        """Insert rows out of order and SELECT * returns all rows."""
        path = self._db_path()
        conn = connect(path)
        conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
        )
        conn.execute(
            "INSERT INTO users (id, name, age) VALUES (3, 'Charlie', 50);"
        )
        conn.execute(
            "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);"
        )
        conn.execute(
            "INSERT INTO users (id, name, age) VALUES (2, 'Bob', 40);"
        )
        result = conn.execute("SELECT * FROM users;")
        self.assertEqual(len(result.rows), 3)
        # Rows should be in rowid order
        self.assertEqual(result.rows[0], [1, "Alice", 30])
        self.assertEqual(result.rows[1], [2, "Bob", 40])
        self.assertEqual(result.rows[2], [3, "Charlie", 50])
        conn.close()

    def test_select_with_where_clause(self):
        """SELECT name FROM users WHERE id = 1 returns the expected row."""
        path = self._db_path()
        conn = connect(path)
        conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
        )
        conn.execute(
            "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);"
        )
        conn.execute(
            "INSERT INTO users (id, name, age) VALUES (2, 'Bob', 40);"
        )
        result = conn.execute("SELECT name FROM users WHERE id = 1;")
        self.assertEqual(result.columns, ["name"])
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0], ["Alice"])
        conn.close()

    def test_data_persists_after_close_reopen(self):
        """Data persists after close and reopen."""
        path = self._db_path()
        conn = connect(path)
        conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
        )
        conn.execute(
            "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);"
        )
        conn.close()

        # Reopen
        conn2 = connect(path)
        result = conn2.execute("SELECT * FROM users;")
        self.assertEqual(result.columns, ["id", "name", "age"])
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0], [1, "Alice", 30])
        conn2.close()

    def test_duplicate_primary_key_raises_error(self):
        """Duplicate primary key raises DuplicateKeyError."""
        path = self._db_path()
        conn = connect(path)
        conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
        )
        conn.execute(
            "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);"
        )
        with self.assertRaises(DuplicateKeyError):
            conn.execute(
                "INSERT INTO users (id, name, age) VALUES (1, 'Bob', 40);"
            )
        conn.close()

    def test_type_mismatch_raises_error(self):
        """Type mismatch raises TypeMismatchError."""
        path = self._db_path()
        conn = connect(path)
        conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
        )
        with self.assertRaises(TypeMismatchError):
            conn.execute(
                "INSERT INTO users (id, name, age) VALUES (1, 123, 30);"
            )
        conn.close()

    def test_delete_row_and_persist(self):
        """DELETE removes row and persists after reopen."""
        path = self._db_path()
        conn = connect(path)
        conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
        )
        conn.execute(
            "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);"
        )
        conn.execute(
            "INSERT INTO users (id, name, age) VALUES (2, 'Bob', 40);"
        )

        # Delete Alice
        result = conn.execute("DELETE FROM users WHERE id = 1;")
        self.assertEqual(result.rows_affected, 1)

        # Verify deletion
        result = conn.execute("SELECT * FROM users;")
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0], [2, "Bob", 40])

        conn.close()

        # Reopen and verify persistence
        conn2 = connect(path)
        result = conn2.execute("SELECT * FROM users;")
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0], [2, "Bob", 40])
        conn2.close()


if __name__ == "__main__":
    unittest.main()