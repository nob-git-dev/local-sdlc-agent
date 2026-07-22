"""
Core Module Tests

Tests for errors.py and result.py to ensure they compile and function correctly.
"""

import unittest

from minisqlite.errors import (
    MiniSQLiteError,
    SQLSyntaxError,
    SchemaError,
    TypeMismatchError,
    DuplicateKeyError,
    StorageError,
    CorruptDatabaseError,
)
from minisqlite.result import Result


class TestErrors(unittest.TestCase):
    """Test error hierarchy and instantiation."""

    def test_base_exception(self):
        """Test that MiniSQLiteError is a subclass of Exception."""
        self.assertTrue(issubclass(MiniSQLiteError, Exception))

    def test_sql_syntax_error(self):
        """Test SQLSyntaxError instantiation."""
        err = SQLSyntaxError("Invalid syntax")
        self.assertIsInstance(err, SQLSyntaxError)
        self.assertIsInstance(err, MiniSQLiteError)
        self.assertEqual(str(err), "Invalid syntax")

    def test_schema_error(self):
        """Test SchemaError instantiation."""
        err = SchemaError("Table not found")
        self.assertIsInstance(err, SchemaError)
        self.assertIsInstance(err, MiniSQLiteError)

    def test_type_mismatch_error(self):
        """Test TypeMismatchError instantiation."""
        err = TypeMismatchError("Expected INTEGER, got TEXT")
        self.assertIsInstance(err, TypeMismatchError)
        self.assertIsInstance(err, MiniSQLiteError)

    def test_duplicate_key_error(self):
        """Test DuplicateKeyError instantiation."""
        err = DuplicateKeyError("Duplicate rowid")
        self.assertIsInstance(err, DuplicateKeyError)
        self.assertIsInstance(err, MiniSQLiteError)

    def test_storage_error(self):
        """Test StorageError instantiation."""
        err = StorageError("I/O error")
        self.assertIsInstance(err, StorageError)
        self.assertIsInstance(err, MiniSQLiteError)

    def test_corrupt_database_error(self):
        """Test CorruptDatabaseError instantiation and inheritance."""
        err = CorruptDatabaseError("Invalid magic bytes")
        self.assertIsInstance(err, CorruptDatabaseError)
        self.assertIsInstance(err, StorageError)
        self.assertIsInstance(err, MiniSQLiteError)


class TestResult(unittest.TestCase):
    """Test Result class."""

    def test_result_with_data(self):
        """Test Result with columns and rows."""
        result = Result(columns=["id", "name"], rows=[[1, "Alice"], [2, "Bob"]])
        self.assertEqual(result.columns, ["id", "name"])
        self.assertEqual(result.rows, [[1, "Alice"], [2, "Bob"]])
        self.assertEqual(result.rowcount, 0)

    def test_result_with_rowcount(self):
        """Test Result with rowcount."""
        result = Result(rowcount=5)
        self.assertEqual(result.columns, [])
        self.assertEqual(result.rows, [])
        self.assertEqual(result.rowcount, 5)

    def test_result_empty(self):
        """Test Result with no arguments."""
        result = Result()
        self.assertEqual(result.columns, [])
        self.assertEqual(result.rows, [])
        self.assertEqual(result.rowcount, 0)

    def test_result_repr(self):
        """Test Result __repr__ method."""
        result = Result(columns=["id"], rows=[[1]], rowcount=1)
        repr_str = repr(result)
        self.assertIn("id", repr_str)
        self.assertIn("1", repr_str)


if __name__ == "__main__":
    unittest.main()
