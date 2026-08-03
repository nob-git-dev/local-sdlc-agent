"""Unit tests for the MiniSQLite SQL parser.

Covers SPEC.md section 16.1 parser cases: CREATE TABLE, INSERT (with
and without a column list), SELECT *, SELECT column list, single WHERE
conditions with all comparison operators, DELETE with WHERE, trailing
semicolons, invalid syntax, and rejection of DELETE without WHERE.
"""

import unittest

from minisqlite.errors import SQLSyntaxError
from minisqlite.sql.ast import (
    ColumnDef,
    Condition,
    CreateTable,
    Delete,
    Insert,
    IntegerLiteral,
    Select,
    TextLiteral,
)
from minisqlite.sql.parser import parse


class TestCreateTable(unittest.TestCase):
    def test_create_table_with_primary_key(self):
        stmt = parse("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);")
        self.assertIsInstance(stmt, CreateTable)
        self.assertEqual(stmt.table_name, "users")
        self.assertEqual(len(stmt.columns), 2)
        self.assertEqual(stmt.columns[0], ColumnDef("id", "INTEGER", True))
        self.assertEqual(stmt.columns[1], ColumnDef("name", "TEXT", False))

    def test_create_table_without_primary_key(self):
        stmt = parse("CREATE TABLE t (a INTEGER, b TEXT);")
        self.assertIsInstance(stmt, CreateTable)
        self.assertEqual(stmt.table_name, "t")
        self.assertEqual(stmt.columns, [ColumnDef("a", "INTEGER"), ColumnDef("b", "TEXT")])

    def test_create_table_keywords_case_insensitive(self):
        stmt = parse("create table users (id integer primary key);")
        self.assertIsInstance(stmt, CreateTable)
        self.assertEqual(stmt.table_name, "users")
        self.assertEqual(stmt.columns[0], ColumnDef("id", "INTEGER", True))


class TestInsert(unittest.TestCase):
    def test_insert_with_column_list(self):
        stmt = parse("INSERT INTO users (id, name) VALUES (1, 'Alice');")
        self.assertIsInstance(stmt, Insert)
        self.assertEqual(stmt.table_name, "users")
        self.assertEqual(stmt.column_names, ["id", "name"])
        self.assertEqual(stmt.values, [IntegerLiteral(1), TextLiteral("Alice")])

    def test_insert_without_column_list(self):
        stmt = parse("INSERT INTO users VALUES (10, 'Bob');")
        self.assertIsInstance(stmt, Insert)
        self.assertEqual(stmt.table_name, "users")
        self.assertEqual(stmt.column_names, [])
        self.assertEqual(stmt.values, [IntegerLiteral(10), TextLiteral("Bob")])

    def test_insert_multiple_values(self):
        stmt = parse("INSERT INTO t (a, b, c) VALUES (1, 2, 3);")
        self.assertIsInstance(stmt, Insert)
        self.assertEqual(stmt.column_names, ["a", "b", "c"])
        self.assertEqual(stmt.values, [IntegerLiteral(1), IntegerLiteral(2), IntegerLiteral(3)])


class TestSelect(unittest.TestCase):
    def test_select_star(self):
        stmt = parse("SELECT * FROM users;")
        self.assertIsInstance(stmt, Select)
        self.assertEqual(stmt.column_names, ["*"])
        self.assertEqual(stmt.table_name, "users")
        self.assertIsNone(stmt.condition)

    def test_select_column_list(self):
        stmt = parse("SELECT id, name FROM users;")
        self.assertIsInstance(stmt, Select)
        self.assertEqual(stmt.column_names, ["id", "name"])
        self.assertEqual(stmt.table_name, "users")
        self.assertIsNone(stmt.condition)

    def test_select_single_column(self):
        stmt = parse("SELECT name FROM users;")
        self.assertIsInstance(stmt, Select)
        self.assertEqual(stmt.column_names, ["name"])
        self.assertEqual(stmt.table_name, "users")


class TestWhereConditions(unittest.TestCase):
    def test_where_equal(self):
        stmt = parse("SELECT * FROM users WHERE id = 1;")
        self.assertIsInstance(stmt, Select)
        self.assertEqual(stmt.condition, Condition("id", "=", IntegerLiteral(1)))

    def test_where_not_equal(self):
        stmt = parse("SELECT * FROM users WHERE id != 2;")
        self.assertEqual(stmt.condition, Condition("id", "!=", IntegerLiteral(2)))

    def test_where_greater(self):
        stmt = parse("SELECT * FROM users WHERE age > 20;")
        self.assertEqual(stmt.condition, Condition("age", ">", IntegerLiteral(20)))

    def test_where_greater_equal(self):
        stmt = parse("SELECT * FROM users WHERE age >= 20;")
        self.assertEqual(stmt.condition, Condition("age", ">=", IntegerLiteral(20)))

    def test_where_less(self):
        stmt = parse("SELECT * FROM users WHERE age < 30;")
        self.assertEqual(stmt.condition, Condition("age", "<", IntegerLiteral(30)))

    def test_where_less_equal(self):
        stmt = parse("SELECT * FROM users WHERE age <= 30;")
        self.assertEqual(stmt.condition, Condition("age", "<=", IntegerLiteral(30)))

    def test_where_text_literal(self):
        stmt = parse("SELECT name FROM users WHERE name = 'Alice';")
        self.assertEqual(stmt.condition, Condition("name", "=", TextLiteral("Alice")))


class TestDelete(unittest.TestCase):
    def test_delete_with_where(self):
        stmt = parse("DELETE FROM users WHERE id = 1;")
        self.assertIsInstance(stmt, Delete)
        self.assertEqual(stmt.table_name, "users")
        self.assertEqual(stmt.condition, Condition("id", "=", IntegerLiteral(1)))

    def test_delete_without_where_rejected(self):
        with self.assertRaises(SQLSyntaxError):
            parse("DELETE FROM users;")


class TestTrailingSemicolon(unittest.TestCase):
    def test_semicolon_required(self):
        stmt = parse("SELECT * FROM users;")
        self.assertIsInstance(stmt, Select)
        with self.assertRaises(SQLSyntaxError):
            parse("SELECT * FROM users")


class TestInvalidSyntax(unittest.TestCase):
    def test_empty_statement(self):
        with self.assertRaises(SQLSyntaxError):
            parse("")

    def test_unknown_keyword(self):
        with self.assertRaises(SQLSyntaxError):
            parse("UPDATE users SET name = 'x';")

    def test_missing_table_name(self):
        with self.assertRaises(SQLSyntaxError):
            parse("CREATE TABLE (id INTEGER);")

    def test_invalid_column_type(self):
        with self.assertRaises(SQLSyntaxError):
            parse("CREATE TABLE t (a FLOAT);")

    def test_missing_values(self):
        with self.assertRaises(SQLSyntaxError):
            parse("INSERT INTO users (id) VALUES;")

    def test_select_without_from(self):
        with self.assertRaises(SQLSyntaxError):
            parse("SELECT id;")

    def test_where_without_operator(self):
        with self.assertRaises(SQLSyntaxError):
            parse("SELECT * FROM users WHERE id 1;")

    def test_where_without_literal(self):
        with self.assertRaises(SQLSyntaxError):
            parse("SELECT * FROM users WHERE id =;")

    def test_unexpected_trailing_token(self):
        with self.assertRaises(SQLSyntaxError):
            parse("SELECT * FROM users extra;")

    def test_unsupported_operator(self):
        with self.assertRaises(SQLSyntaxError):
            parse("SELECT * FROM users WHERE id LIKE 1;")


if __name__ == "__main__":
    unittest.main()