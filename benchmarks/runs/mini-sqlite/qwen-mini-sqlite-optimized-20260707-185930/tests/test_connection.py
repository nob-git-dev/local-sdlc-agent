"""
Connection API Tests

Tests for the main connection and execution API.
"""

import unittest
import os
import tempfile

from minisqlite.connection import connect, Connection
from minisqlite.errors import (
    MiniSQLiteError,
    SchemaError,
    TypeMismatchError,
    SQLSyntaxError,
)
from minisqlite.result import Result


class TestConnectionCreation(unittest.TestCase):
    """Test connection creation and basic properties."""

    def setUp(self):
        """Create a temporary database file."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

    def tearDown(self):
        """Clean up temporary files."""
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)

    def test_connection_creation(self):
        """Test that a connection can be created."""
        conn = connect(self.db_path)
        self.assertIsInstance(conn, Connection)
        conn.close()

    def test_context_manager(self):
        """Test that connection works as a context manager."""
        with connect(self.db_path) as conn:
            self.assertFalse(conn._closed)
        self.assertTrue(conn._closed)


class TestCreateTable(unittest.TestCase):
    """Test CREATE TABLE functionality."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)

    def test_create_table(self):
        """Test creating a table."""
        with connect(self.db_path) as conn:
            result = conn.execute(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
            )
            self.assertIsInstance(result, Result)
            self.assertEqual(result.rowcount, 0)

    def test_create_table_duplicate(self):
        """Test that creating a duplicate table raises an error."""
        with connect(self.db_path) as conn:
            conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);")
            with self.assertRaises(SchemaError):
                conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY);")


class TestInsert(unittest.TestCase):
    """Test INSERT functionality."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)

    def test_insert_row(self):
        """Test inserting a row."""
        with connect(self.db_path) as conn:
            conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);")
            result = conn.execute(
                "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);"
            )
            self.assertEqual(result.rowcount, 1)

    def test_insert_type_mismatch(self):
        """Test that inserting wrong type raises error."""
        with connect(self.db_path) as conn:
            conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);")
            with self.assertRaises(TypeMismatchError):
                conn.execute("INSERT INTO users (id, name) VALUES (1, 123);")

    def test_insert_missing_table(self):
        """Test inserting into non-existent table raises error."""
        with connect(self.db_path) as conn:
            with self.assertRaises(SchemaError):
                conn.execute("INSERT INTO nonexistent (id) VALUES (1);")


class TestSelect(unittest.TestCase):
    """Test SELECT functionality."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)

    def test_select_all(self):
        """Test SELECT *."""
        with connect(self.db_path) as conn:
            conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);")
            conn.execute("INSERT INTO users (id, name) VALUES (1, 'Alice');")
            result = conn.execute("SELECT * FROM users;")
            self.assertEqual(result.columns, ["id", "name"])
            self.assertEqual(len(result.rows), 1)
            self.assertEqual(result.rows[0], [1, "Alice"])

    def test_select_columns(self):
        """Test selecting specific columns."""
        with connect(self.db_path) as conn:
            conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);")
            conn.execute("INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);")
            result = conn.execute("SELECT name FROM users;")
            self.assertEqual(result.columns, ["name"])
            self.assertEqual(result.rows, [["Alice"]])

    def test_select_where(self):
        """Test SELECT with WHERE clause."""
        with connect(self.db_path) as conn:
            conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);")
            conn.execute("INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);")
            conn.execute("INSERT INTO users (id, name, age) VALUES (2, 'Bob', 25);")
            result = conn.execute("SELECT name FROM users WHERE age >= 30;")
            self.assertEqual(result.rows, [["Alice"]])

    def test_select_where_no_match(self):
        """Test SELECT with WHERE that matches nothing."""
        with connect(self.db_path) as conn:
            conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);")
            conn.execute("INSERT INTO users (id, name) VALUES (1, 'Alice');")
            result = conn.execute("SELECT * FROM users WHERE id = 999;")
            self.assertEqual(result.rows, [])

    def test_select_missing_table(self):
        """Test selecting from non-existent table raises error."""
        with connect(self.db_path) as conn:
            with self.assertRaises(SchemaError):
                conn.execute("SELECT * FROM nonexistent;")


class TestDelete(unittest.TestCase):
    """Test DELETE functionality."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)

    def test_delete_with_where(self):
        """Test DELETE with WHERE clause."""
        with connect(self.db_path) as conn:
            conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);")
            conn.execute("INSERT INTO users (id, name) VALUES (1, 'Alice');")
            conn.execute("INSERT INTO users (id, name) VALUES (2, 'Bob');")
            result = conn.execute("DELETE FROM users WHERE id = 1;")
            self.assertEqual(result.rowcount, 1)

            # Verify deletion
            result = conn.execute("SELECT * FROM users;")
            self.assertEqual(len(result.rows), 1)
            self.assertEqual(result.rows[0][1], "Bob")

    def test_delete_without_where(self):
        """Test that DELETE without WHERE raises error."""
        with connect(self.db_path) as conn:
            conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);")
            with self.assertRaises(SQLSyntaxError):
                conn.execute("DELETE FROM users;")

    def test_delete_missing_table(self):
        """Test deleting from non-existent table raises error."""
        with connect(self.db_path) as conn:
            with self.assertRaises(SchemaError):
                conn.execute("DELETE FROM nonexistent WHERE id = 1;")


class TestPersistence(unittest.TestCase):
    """Test data persistence across connections."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)

    def test_persistence(self):
        """Test that data persists after closing and reopening."""
        # First connection: create and insert
        conn1 = connect(self.db_path)
        conn1.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);")
        conn1.execute("INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);")
        conn1.execute("INSERT INTO users (id, name, age) VALUES (2, 'Bob', 25);")
        conn1.close()

        # Second connection: verify data
        conn2 = connect(self.db_path)
        result = conn2.execute("SELECT * FROM users;")
        self.assertEqual(len(result.rows), 2)
        self.assertEqual(result.rows[0], [1, "Alice", 30])
        self.assertEqual(result.rows[1], [2, "Bob", 25])
        conn2.close()

    def test_persistence_after_delete(self):
        """Test that deletions persist after closing and reopening."""
        conn1 = connect(self.db_path)
        conn1.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);")
        conn1.execute("INSERT INTO users (id, name) VALUES (1, 'Alice');")
        conn1.execute("INSERT INTO users (id, name) VALUES (2, 'Bob');")
        conn1.execute("DELETE FROM users WHERE id = 1;")
        conn1.close()

        conn2 = connect(self.db_path)
        result = conn2.execute("SELECT * FROM users;")
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0][1], "Bob")
        conn2.close()


class TestErrorHandling(unittest.TestCase):
    """Test error handling."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)

    def test_closed_connection(self):
        """Test that executing on closed connection raises error."""
        conn = connect(self.db_path)
        conn.close()
        with self.assertRaises(MiniSQLiteError):
            conn.execute("SELECT * FROM users;")

    def test_syntax_error(self):
        """Test that invalid SQL raises SQLSyntaxError."""
        with connect(self.db_path) as conn:
            with self.assertRaises(SQLSyntaxError):
                conn.execute("SELEC * FROM users;")