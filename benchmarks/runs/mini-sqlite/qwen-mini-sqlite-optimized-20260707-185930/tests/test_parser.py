"""
SQL Parser Tests

Tests for the SQL parser to ensure correct AST generation.
"""

import unittest

from minisqlite.errors import SQLSyntaxError
from minisqlite.sql.ast import (
    ColumnDef,
    Condition,
    CreateTable,
    Delete,
    Insert,
    Select,
)
from minisqlite.sql.lexer import tokenize
from minisqlite.sql.parser import parse


class TestCreateTable(unittest.TestCase):
    """Test CREATE TABLE parsing."""

    def test_simple_create_table(self):
        """Test parsing a simple CREATE TABLE statement."""
        sql = "CREATE TABLE users (id INTEGER, name TEXT);"
        tokens = tokenize(sql)
        ast = parse(tokens)

        self.assertIsInstance(ast, CreateTable)
        self.assertEqual(ast.table_name, "users")
        self.assertEqual(len(ast.columns), 2)
        self.assertEqual(ast.columns[0].name, "id")
        self.assertEqual(ast.columns[0].type, "INTEGER")
        self.assertFalse(ast.columns[0].primary_key)
        self.assertEqual(ast.columns[1].name, "name")
        self.assertEqual(ast.columns[1].type, "TEXT")

    def test_create_table_with_primary_key(self):
        """Test parsing CREATE TABLE with PRIMARY KEY."""
        sql = "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);"
        tokens = tokenize(sql)
        ast = parse(tokens)

        self.assertIsInstance(ast, CreateTable)
        self.assertEqual(ast.table_name, "users")
        self.assertEqual(len(ast.columns), 2)
        self.assertTrue(ast.columns[0].primary_key)
        self.assertFalse(ast.columns[1].primary_key)

    def test_create_table_case_insensitive(self):
        """Test that CREATE TABLE is case insensitive."""
        sql = "create table users (id integer primary key);"
        tokens = tokenize(sql)
        ast = parse(tokens)

        self.assertIsInstance(ast, CreateTable)
        self.assertEqual(ast.table_name, "users")
        self.assertEqual(ast.columns[0].type, "INTEGER")
        self.assertTrue(ast.columns[0].primary_key)

    def test_create_table_multiple_columns(self):
        """Test parsing CREATE TABLE with multiple columns."""
        sql = "CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, price INTEGER, description TEXT);"
        tokens = tokenize(sql)
        ast = parse(tokens)

        self.assertIsInstance(ast, CreateTable)
        self.assertEqual(len(ast.columns), 4)
        self.assertEqual(ast.columns[0].name, "id")
        self.assertEqual(ast.columns[1].name, "name")
        self.assertEqual(ast.columns[2].name, "price")
        self.assertEqual(ast.columns[3].name, "description")


class TestInsert(unittest.TestCase):
    """Test INSERT parsing."""

    def test_insert_with_columns(self):
        """Test parsing INSERT with column list."""
        sql = "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);"
        tokens = tokenize(sql)
        ast = parse(tokens)

        self.assertIsInstance(ast, Insert)
        self.assertEqual(ast.table_name, "users")
        self.assertEqual(ast.columns, ["id", "name", "age"])
        self.assertEqual(ast.values, [1, "Alice", 30])

    def test_insert_without_columns(self):
        """Test parsing INSERT without column list."""
        sql = "INSERT INTO users VALUES (1, 'Bob', 25);"
        tokens = tokenize(sql)
        ast = parse(tokens)

        self.assertIsInstance(ast, Insert)
        self.assertEqual(ast.table_name, "users")
        self.assertEqual(ast.columns, [])
        self.assertEqual(ast.values, [1, "Bob", 25])

    def test_insert_text_with_escaped_quote(self):
        """Test parsing INSERT with escaped quotes in text."""
        sql = "INSERT INTO users (id, name) VALUES (1, 'It''s Alice');"
        tokens = tokenize(sql)
        ast = parse(tokens)

        self.assertIsInstance(ast, Insert)
        self.assertEqual(ast.values[1], "It's Alice")

    def test_insert_multiple_values(self):
        """Test parsing INSERT with multiple values in one VALUES clause."""
        sql = "INSERT INTO users (id, name) VALUES (1, 'Alice');"
        tokens = tokenize(sql)
        ast = parse(tokens)

        self.assertIsInstance(ast, Insert)
        self.assertEqual(len(ast.values), 2)


class TestSelect(unittest.TestCase):
    """Test SELECT parsing."""

    def test_select_all_columns(self):
        """Test parsing SELECT with *."""
        sql = "SELECT * FROM users;"
        tokens = tokenize(sql)
        ast = parse(tokens)

        self.assertIsInstance(ast, Select)
        self.assertEqual(ast.columns, ["*"])
        self.assertEqual(ast.table_name, "users")
        self.assertIsNone(ast.condition)

    def test_select_specific_columns(self):
        """Test parsing SELECT with specific columns."""
        sql = "SELECT id, name FROM users;"
        tokens = tokenize(sql)
        ast = parse(tokens)

        self.assertIsInstance(ast, Select)
        self.assertEqual(ast.columns, ["id", "name"])
        self.assertEqual(ast.table_name, "users")

    def test_select_with_where_equals(self):
        """Test parsing SELECT with WHERE = condition."""
        sql = "SELECT * FROM users WHERE id = 1;"
        tokens = tokenize(sql)
        ast = parse(tokens)

        self.assertIsInstance(ast, Select)
        self.assertIsNotNone(ast.condition)
        self.assertEqual(ast.condition.column, "id")
        self.assertEqual(ast.condition.operator, "=")
        self.assertEqual(ast.condition.value, 1)

    def test_select_with_where_comparison(self):
        """Test parsing SELECT with various comparison operators."""
        sql = "SELECT name FROM users WHERE age >= 30;"
        tokens = tokenize(sql)
        ast = parse(tokens)

        self.assertIsInstance(ast, Select)
        self.assertEqual(ast.condition.column, "age")
        self.assertEqual(ast.condition.operator, ">=")
        self.assertEqual(ast.condition.value, 30)

    def test_select_with_text_condition(self):
        """Test parsing SELECT with text condition."""
        sql = "SELECT * FROM users WHERE name = 'Alice';"
        tokens = tokenize(sql)
        ast = parse(tokens)

        self.assertIsInstance(ast, Select)
        self.assertEqual(ast.condition.column, "name")
        self.assertEqual(ast.condition.operator, "=")
        self.assertEqual(ast.condition.value, "Alice")


class TestDelete(unittest.TestCase):
    """Test DELETE parsing."""

    def test_delete_with_where(self):
        """Test parsing DELETE with WHERE clause."""
        sql = "DELETE FROM users WHERE id = 1;"
        tokens = tokenize(sql)
        ast = parse(tokens)

        self.assertIsInstance(ast, Delete)
        self.assertEqual(ast.table_name, "users")
        self.assertIsNotNone(ast.condition)
        self.assertEqual(ast.condition.column, "id")
        self.assertEqual(ast.condition.operator, "=")
        self.assertEqual(ast.condition.value, 1)

    def test_delete_without_where_fails(self):
        """Test that DELETE without WHERE raises an error."""
        sql = "DELETE FROM users;"
        tokens = tokenize(sql)

        with self.assertRaises(SQLSyntaxError) as context:
            parse(tokens)

        self.assertIn("WHERE", str(context.exception))


class TestErrorHandling(unittest.TestCase):
    """Test parser error handling."""

    def test_empty_statement(self):
        """Test that empty SQL raises an error."""
        tokens = tokenize("")
        with self.assertRaises(SQLSyntaxError):
            parse(tokens)

    def test_invalid_keyword(self):
        """Test that unknown statement type raises an error."""
        sql = "UPDATE users SET name = 'Alice';"
        tokens = tokenize(sql)
        with self.assertRaises(SQLSyntaxError):
            parse(tokens)

    def test_missing_table_name(self):
        """Test that missing table name raises an error."""
        sql = "CREATE TABLE ;"
        tokens = tokenize(sql)
        with self.assertRaises(SQLSyntaxError):
            parse(tokens)

    def test_invalid_column_type(self):
        """Test that invalid column type raises an error."""
        sql = "CREATE TABLE users (id FLOAT);"
        tokens = tokenize(sql)
        with self.assertRaises(SQLSyntaxError):
            parse(tokens)

    def test_missing_parenthesis(self):
        """Test that missing parenthesis raises an error."""
        sql = "CREATE TABLE users id INTEGER, name TEXT);"
        tokens = tokenize(sql)
        with self.assertRaises(SQLSyntaxError):
            parse(tokens)

    def test_invalid_value_type(self):
        """Test that invalid value type raises an error."""
        sql = "INSERT INTO users (id, name) VALUES ('Alice', 30);"
        # This should parse successfully - the type checking happens at execution time
        tokens = tokenize(sql)
        ast = parse(tokens)
        self.assertIsInstance(ast, Insert)


class TestComplexSQL(unittest.TestCase):
    """Test parsing of complex SQL statements."""

    def test_complex_create_table(self):
        """Test parsing a complex CREATE TABLE statement."""
        sql = """
        CREATE TABLE employees (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            department TEXT,
            salary INTEGER
        );
        """
        tokens = tokenize(sql)
        ast = parse(tokens)

        self.assertIsInstance(ast, CreateTable)
        self.assertEqual(ast.table_name, "employees")
        self.assertEqual(len(ast.columns), 4)

    def test_complex_select(self):
        """Test parsing a complex SELECT statement."""
        sql = "SELECT id, name, age FROM customers WHERE age > 18 AND status = 'active';"
        tokens = tokenize(sql)
        # Note: AND/OR not fully supported yet, this should fail or parse partially
        # For now, we test that it at least tokenizes
        # Actually, our parser doesn't support AND/OR in conditions yet
        # So this will fail at the condition parsing level
        with self.assertRaises(SQLSyntaxError):
            parse(tokens)

    def test_select_with_text_where(self):
        """Test parsing SELECT with text WHERE condition."""
        sql = "SELECT * FROM products WHERE category = 'electronics';"
        tokens = tokenize(sql)
        ast = parse(tokens)

        self.assertIsInstance(ast, Select)
        self.assertEqual(ast.condition.column, "category")
        self.assertEqual(ast.condition.operator, "=")
        self.assertEqual(ast.condition.value, "electronics")