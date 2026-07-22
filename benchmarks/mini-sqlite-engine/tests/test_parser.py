import unittest
from minisqlite.errors import SQLSyntaxError
from minisqlite.sql.parser import parse
from minisqlite.sql.ast import (
    ColumnDef,
    Condition,
    CreateTable,
    Delete,
    Insert,
    Literal,
    Select,
)


class TestCreateTable(unittest.TestCase):
    def test_create_table_parses_table_name_and_columns(self):
        result = parse("CREATE TABLE users (id INTEGER, name TEXT);")
        self.assertIsInstance(result, CreateTable)
        self.assertEqual(result.table_name, "users")
        self.assertEqual(len(result.columns), 2)
        self.assertEqual(result.columns[0].name, "id")
        self.assertEqual(result.columns[0].type_name, "INTEGER")
        self.assertFalse(result.columns[0].primary_key)
        self.assertEqual(result.columns[1].name, "name")
        self.assertEqual(result.columns[1].type_name, "TEXT")

    def test_create_table_parses_primary_key(self):
        result = parse("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);")
        self.assertIsInstance(result, CreateTable)
        self.assertEqual(result.columns[0].primary_key, True)
        self.assertEqual(result.columns[0].type_name, "INTEGER")

    def test_create_table_single_column(self):
        result = parse("CREATE TABLE t (x INTEGER);")
        self.assertIsInstance(result, CreateTable)
        self.assertEqual(len(result.columns), 1)
        self.assertEqual(result.columns[0].name, "x")


class TestInsert(unittest.TestCase):
    def test_insert_with_column_list(self):
        result = parse(
            "INSERT INTO users (id, name) VALUES (1, 'Alice');"
        )
        self.assertIsInstance(result, Insert)
        self.assertEqual(result.table_name, "users")
        self.assertEqual(result.columns, ["id", "name"])
        self.assertEqual(len(result.values), 2)
        self.assertIsInstance(result.values[0], Literal)
        self.assertEqual(result.values[0].value, 1)
        self.assertEqual(result.values[0].type_name, "INTEGER")
        self.assertEqual(result.values[1].value, "Alice")
        self.assertEqual(result.values[1].type_name, "TEXT")

    def test_insert_without_column_list(self):
        result = parse("INSERT INTO users VALUES (1, 'Alice');")
        self.assertIsInstance(result, Insert)
        self.assertEqual(result.table_name, "users")
        self.assertIsNone(result.columns)
        self.assertEqual(len(result.values), 2)

    def test_insert_single_value(self):
        result = parse("INSERT INTO t VALUES (42);")
        self.assertIsInstance(result, Insert)
        self.assertEqual(len(result.values), 1)
        self.assertEqual(result.values[0].value, 42)


class TestSelect(unittest.TestCase):
    def test_select_star(self):
        result = parse("SELECT * FROM users;")
        self.assertIsInstance(result, Select)
        self.assertEqual(result.table_name, "users")
        self.assertIsNone(result.columns)
        self.assertIsNone(result.where)

    def test_select_column_list(self):
        result = parse("SELECT id, name FROM users;")
        self.assertIsInstance(result, Select)
        self.assertEqual(result.columns, ["id", "name"])
        self.assertIsNone(result.where)

    def test_select_with_where(self):
        result = parse("SELECT * FROM users WHERE id = 1;")
        self.assertIsInstance(result, Select)
        self.assertIsNotNone(result.where)
        self.assertEqual(result.where.column, "id")
        self.assertEqual(result.where.operator, "=")
        self.assertIsInstance(result.where.value, Literal)
        self.assertEqual(result.where.value.value, 1)


class TestDelete(unittest.TestCase):
    def test_delete_with_where(self):
        result = parse("DELETE FROM users WHERE id = 1;")
        self.assertIsInstance(result, Delete)
        self.assertEqual(result.table_name, "users")
        self.assertEqual(result.where.column, "id")
        self.assertEqual(result.where.operator, "=")
        self.assertEqual(result.where.value.value, 1)

    def test_delete_without_where_raises(self):
        with self.assertRaises(SQLSyntaxError):
            parse("DELETE FROM users;")


class TestConditions(unittest.TestCase):
    def test_condition_operators(self):
        operators = ["=", "!=", ">", ">=", "<", "<="]
        for op in operators:
            sql = f"SELECT * FROM t WHERE x {op} 1;"
            result = parse(sql)
            self.assertEqual(result.where.operator, op)

    def test_condition_with_string_literal(self):
        result = parse("SELECT * FROM t WHERE name = 'hello';")
        self.assertEqual(result.where.value.value, "hello")
        self.assertEqual(result.where.value.type_name, "TEXT")


class TestInvalidSyntax(unittest.TestCase):
    def test_missing_semicolon_raises(self):
        with self.assertRaises(SQLSyntaxError):
            parse("SELECT * FROM users")

    def test_unknown_keyword_raises(self):
        with self.assertRaises(SQLSyntaxError):
            parse("DROP TABLE users;")

    def test_invalid_column_type_raises(self):
        with self.assertRaises(SQLSyntaxError):
            parse("CREATE TABLE t (x FLOAT);")

    def test_missing_values_raises(self):
        with self.assertRaises(SQLSyntaxError):
            parse("INSERT INTO t VALUES;")


if __name__ == "__main__":
    unittest.main()
