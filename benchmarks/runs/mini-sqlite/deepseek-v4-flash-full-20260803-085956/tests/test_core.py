"""Tests for shared errors and result container (S01)."""

import unittest

from minisqlite.errors import (
    CorruptDatabaseError,
    DuplicateKeyError,
    MiniSQLiteError,
    SchemaError,
    SQLSyntaxError,
    StorageError,
    TypeMismatchError,
)
from minisqlite.result import Result


class ErrorHierarchyTest(unittest.TestCase):
    """Verify the exception hierarchy from SPEC.md section 15.1."""

    def test_all_errors_derive_from_base(self):
        for error_type in (
            SQLSyntaxError,
            SchemaError,
            TypeMismatchError,
            DuplicateKeyError,
            StorageError,
            CorruptDatabaseError,
        ):
            self.assertTrue(issubclass(error_type, MiniSQLiteError))

    def test_corrupt_database_is_storage_error(self):
        self.assertTrue(issubclass(CorruptDatabaseError, StorageError))

    def test_exceptions_are_catchable_as_base(self):
        with self.assertRaises(MiniSQLiteError):
            raise SchemaError("missing table")


class ResultTest(unittest.TestCase):
    """Verify the Result container behavior."""

    def test_columns_and_rows_are_stored(self):
        result = Result(["id", "name"], [[1, "Alice"]])
        self.assertEqual(result.columns, ["id", "name"])
        self.assertEqual(result.rows, [[1, "Alice"]])

    def test_columns_and_rows_are_copied(self):
        columns = ["id"]
        rows = [[1]]
        result = Result(columns, rows)
        columns.append("name")
        rows[0].append("Alice")
        self.assertEqual(result.columns, ["id"])
        self.assertEqual(result.rows, [[1]])

    def test_equality_compares_columns_and_rows(self):
        first = Result(["id"], [[1]])
        second = Result(["id"], [[1]])
        self.assertEqual(first, second)

    def test_inequality_detects_different_columns(self):
        first = Result(["id"], [[1]])
        second = Result(["name"], [["Alice"]])
        self.assertNotEqual(first, second)

    def test_inequality_detects_different_rows(self):
        first = Result(["id"], [[1]])
        second = Result(["id"], [[2]])
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
