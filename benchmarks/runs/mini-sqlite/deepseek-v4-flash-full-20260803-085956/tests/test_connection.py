#!/usr/bin/env python3
"""Connection-level acceptance tests for the MiniSQLite Engine.

These tests exercise the public API (connect / Connection / Result)
against a temporary database file.  They cover CREATE TABLE, INSERT,
SELECT, WHERE, DELETE, persistence across close/reopen, and error
cases.  They are independent of any external reference implementation.
"""

import os
import tempfile
import unittest

from minisqlite import connect
from minisqlite.errors import (
    DuplicateKeyError,
    SchemaError,
    SQLSyntaxError,
    TypeMismatchError,
)


class ConnectionAcceptanceTest(unittest.TestCase):
    """Acceptance tests driven by SPEC.md section 23."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._tmpdir.name, "test.db")

    def tearDown(self):
        self._tmpdir.cleanup()

    def _open(self):
        return connect(self._db_path)

    def test_create_table_succeeds(self):
        conn = self._open()
        try:
            result = conn.execute(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
            )
            self.assertEqual(result.columns, [])
            self.assertEqual(result.rows, [])
        finally:
            conn.close()

    def test_insert_returns_ok(self):
        conn = self._open()
        try:
            conn.execute(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
            )
            result = conn.execute(
                "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);"
            )
            self.assertEqual(result.columns, [])
            self.assertEqual(result.rows, [])
        finally:
            conn.close()

    def test_select_returns_inserted_row(self):
        conn = self._open()
        try:
            conn.execute(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
            )
            conn.execute(
                "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);"
            )
            result = conn.execute("SELECT * FROM users;")
            self.assertEqual(result.columns, ["id", "name", "age"])
            self.assertEqual(result.rows, [[1, "Alice", 30]])
        finally:
            conn.close()

    def test_where_filter_works(self):
        conn = self._open()
        try:
            conn.execute(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
            )
            conn.execute(
                "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);"
            )
            conn.execute(
                "INSERT INTO users (id, name, age) VALUES (2, 'Bob', 25);"
            )
            conn.execute(
                "INSERT INTO users (id, name, age) VALUES (3, 'Carol', 41);"
            )
            result = conn.execute("SELECT name FROM users WHERE age >= 30;")
            self.assertEqual(result.columns, ["name"])
            self.assertEqual(result.rows, [["Alice"], ["Carol"]])
        finally:
            conn.close()

    def test_delete_removes_row(self):
        conn = self._open()
        try:
            conn.execute(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
            )
            conn.execute(
                "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);"
            )
            conn.execute(
                "INSERT INTO users (id, name, age) VALUES (2, 'Bob', 25);"
            )
            result = conn.execute("DELETE FROM users WHERE id = 1;")
            self.assertEqual(result.rows_affected, 1)
            remaining = conn.execute("SELECT * FROM users;")
            self.assertEqual(remaining.rows, [[2, "Bob", 25]])
        finally:
            conn.close()

    def test_deleted_row_not_returned_after_delete(self):
        conn = self._open()
        try:
            conn.execute(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
            )
            conn.execute(
                "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);"
            )
            conn.execute("DELETE FROM users WHERE id = 1;")
            result = conn.execute("SELECT * FROM users;")
            self.assertEqual(result.rows, [])
        finally:
            conn.close()

    def test_persistence_across_close_and_reopen(self):
        conn = self._open()
        conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
        )
        conn.execute(
            "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);"
        )
        conn.close()

        conn2 = self._open()
        try:
            result = conn2.execute("SELECT * FROM users;")
            self.assertEqual(result.columns, ["id", "name", "age"])
            self.assertEqual(result.rows, [[1, "Alice", 30]])
        finally:
            conn2.close()

    def test_persistence_after_delete(self):
        conn = self._open()
        conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
        )
        conn.execute(
            "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);"
        )
        conn.execute(
            "INSERT INTO users (id, name, age) VALUES (2, 'Bob', 25);"
        )
        conn.execute("DELETE FROM users WHERE id = 1;")
        conn.close()

        conn2 = self._open()
        try:
            result = conn2.execute("SELECT * FROM users;")
            self.assertEqual(result.rows, [[2, "Bob", 25]])
        finally:
            conn2.close()

    def test_duplicate_rowid_raises(self):
        conn = self._open()
        try:
            conn.execute(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
            )
            conn.execute(
                "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);"
            )
            with self.assertRaises(DuplicateKeyError):
                conn.execute(
                    "INSERT INTO users (id, name, age) VALUES (1, 'Bob', 25);"
                )
        finally:
            conn.close()

    def test_type_mismatch_raises(self):
        conn = self._open()
        try:
            conn.execute(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
            )
            with self.assertRaises(TypeMismatchError):
                conn.execute(
                    "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 'not-an-int');"
                )
        finally:
            conn.close()

    def test_missing_table_raises(self):
        conn = self._open()
        try:
            with self.assertRaises(SchemaError):
                conn.execute("SELECT * FROM missing_table;")
        finally:
            conn.close()

    def test_missing_column_raises(self):
        conn = self._open()
        try:
            conn.execute(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
            )
            with self.assertRaises(SchemaError):
                conn.execute("SELECT nope FROM users;")
        finally:
            conn.close()

    def test_invalid_sql_raises(self):
        conn = self._open()
        try:
            with self.assertRaises(SQLSyntaxError):
                conn.execute("THIS IS NOT SQL;")
        finally:
            conn.close()

    def test_delete_without_where_raises(self):
        conn = self._open()
        try:
            conn.execute(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
            )
            with self.assertRaises(SQLSyntaxError):
                conn.execute("DELETE FROM users;")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
