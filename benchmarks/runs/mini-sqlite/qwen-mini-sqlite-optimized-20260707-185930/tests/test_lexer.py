"""
SQL Lexer Tests

Tests for the SQL lexer to ensure correct tokenization.
"""

import unittest

from minisqlite.errors import SQLSyntaxError
from minisqlite.sql.lexer import tokenize, Token, TokenType


class TestKeywordRecognition(unittest.TestCase):
    """Test keyword recognition."""

    def test_create_keyword(self):
        """Test that CREATE is recognized as a keyword."""
        tokens = tokenize("CREATE")
        self.assertEqual(len(tokens), 2)  # Token + EOF
        self.assertEqual(tokens[0].type, TokenType.KEYWORD)
        self.assertEqual(tokens[0].value, "CREATE")

    def test_table_keyword(self):
        """Test that TABLE is recognized as a keyword."""
        tokens = tokenize("TABLE")
        self.assertEqual(tokens[0].type, TokenType.KEYWORD)
        self.assertEqual(tokens[0].value, "TABLE")

    def test_insert_keyword(self):
        """Test that INSERT is recognized as a keyword."""
        tokens = tokenize("INSERT")
        self.assertEqual(tokens[0].type, TokenType.KEYWORD)
        self.assertEqual(tokens[0].value, "INSERT")

    def test_select_keyword(self):
        """Test that SELECT is recognized as a keyword."""
        tokens = tokenize("SELECT")
        self.assertEqual(tokens[0].type, TokenType.KEYWORD)
        self.assertEqual(tokens[0].value, "SELECT")

    def test_from_keyword(self):
        """Test that FROM is recognized as a keyword."""
        tokens = tokenize("FROM")
        self.assertEqual(tokens[0].type, TokenType.KEYWORD)
        self.assertEqual(tokens[0].value, "FROM")

    def test_delete_keyword(self):
        """Test that DELETE is recognized as a keyword."""
        tokens = tokenize("DELETE")
        self.assertEqual(tokens[0].type, TokenType.KEYWORD)
        self.assertEqual(tokens[0].value, "DELETE")

    def test_where_keyword(self):
        """Test that WHERE is recognized as a keyword."""
        tokens = tokenize("WHERE")
        self.assertEqual(tokens[0].type, TokenType.KEYWORD)
        self.assertEqual(tokens[0].value, "WHERE")

    def test_integer_keyword(self):
        """Test that INTEGER is recognized as a keyword."""
        tokens = tokenize("INTEGER")
        self.assertEqual(tokens[0].type, TokenType.KEYWORD)
        self.assertEqual(tokens[0].value, "INTEGER")

    def test_text_keyword(self):
        """Test that TEXT is recognized as a keyword."""
        tokens = tokenize("TEXT")
        self.assertEqual(tokens[0].type, TokenType.KEYWORD)
        self.assertEqual(tokens[0].value, "TEXT")


class TestIdentifierRecognition(unittest.TestCase):
    """Test identifier recognition."""

    def test_simple_identifier(self):
        """Test that 'users' is recognized as an identifier."""
        tokens = tokenize("users")
        self.assertEqual(tokens[0].type, TokenType.IDENTIFIER)
        self.assertEqual(tokens[0].value, "users")

    def test_identifier_with_underscore(self):
        """Test that 'user_name' is recognized as an identifier."""
        tokens = tokenize("user_name")
        self.assertEqual(tokens[0].type, TokenType.IDENTIFIER)
        self.assertEqual(tokens[0].value, "user_name")

    def test_identifier_with_number(self):
        """Test that 'table1' is recognized as an identifier."""
        tokens = tokenize("table1")
        self.assertEqual(tokens[0].type, TokenType.IDENTIFIER)
        self.assertEqual(tokens[0].value, "table1")

    def test_identifier_starting_with_underscore(self):
        """Test that '_id' is recognized as an identifier."""
        tokens = tokenize("_id")
        self.assertEqual(tokens[0].type, TokenType.IDENTIFIER)
        self.assertEqual(tokens[0].value, "_id")


class TestIntegerLiteral(unittest.TestCase):
    """Test integer literal recognition."""

    def test_positive_integer(self):
        """Test that 123 is recognized as an integer."""
        tokens = tokenize("123")
        self.assertEqual(tokens[0].type, TokenType.INTEGER)
        self.assertEqual(tokens[0].value, 123)

    def test_zero(self):
        """Test that 0 is recognized as an integer."""
        tokens = tokenize("0")
        self.assertEqual(tokens[0].type, TokenType.INTEGER)
        self.assertEqual(tokens[0].value, 0)

    def test_negative_number_tokens(self):
        """Test that -10 is tokenized as '-' and '10' (per spec D7)."""
        tokens = tokenize("-10")
        self.assertEqual(len(tokens), 3)  # '-', '10', EOF
        self.assertEqual(tokens[0].type, TokenType.OPERATOR)
        self.assertEqual(tokens[0].value, "-")
        self.assertEqual(tokens[1].type, TokenType.INTEGER)
        self.assertEqual(tokens[1].value, 10)


class TestTextLiteral(unittest.TestCase):
    """Test text literal recognition."""

    def test_simple_string(self):
        """Test that 'hello' is recognized as a text literal."""
        tokens = tokenize("'hello'")
        self.assertEqual(tokens[0].type, TokenType.TEXT)
        self.assertEqual(tokens[0].value, "hello")

    def test_string_with_spaces(self):
        """Test that 'hello world' is recognized as a text literal."""
        tokens = tokenize("'hello world'")
        self.assertEqual(tokens[0].type, TokenType.TEXT)
        self.assertEqual(tokens[0].value, "hello world")

    def test_escaped_quote(self):
        """Test that 'It''s OK' is correctly unescaped to "It's OK"."""
        tokens = tokenize("'It''s OK'")
        self.assertEqual(tokens[0].type, TokenType.TEXT)
        self.assertEqual(tokens[0].value, "It's OK")

    def test_empty_string(self):
        """Test that '' is recognized as an empty text literal."""
        tokens = tokenize("''")
        self.assertEqual(tokens[0].type, TokenType.TEXT)
        self.assertEqual(tokens[0].value, "")

    def test_unicode_string(self):
        """Test that UTF-8 strings are handled correctly."""
        tokens = tokenize("'こんにちは'")
        self.assertEqual(tokens[0].type, TokenType.TEXT)
        self.assertEqual(tokens[0].value, "こんにちは")


class TestOperatorRecognition(unittest.TestCase):
    """Test operator recognition."""

    def test_equals(self):
        """Test that = is recognized as an operator."""
        tokens = tokenize("=")
        self.assertEqual(tokens[0].type, TokenType.OPERATOR)
        self.assertEqual(tokens[0].value, "=")

    def test_not_equals(self):
        """Test that != is recognized as an operator."""
        tokens = tokenize("!=")
        self.assertEqual(tokens[0].type, TokenType.OPERATOR)
        self.assertEqual(tokens[0].value, "!=")

    def test_less_than(self):
        """Test that < is recognized as an operator."""
        tokens = tokenize("<")
        self.assertEqual(tokens[0].type, TokenType.OPERATOR)
        self.assertEqual(tokens[0].value, "<")

    def test_greater_than(self):
        """Test that > is recognized as an operator."""
        tokens = tokenize(">")
        self.assertEqual(tokens[0].type, TokenType.OPERATOR)
        self.assertEqual(tokens[0].value, ">")

    def test_less_than_equal(self):
        """Test that <= is recognized as an operator."""
        tokens = tokenize("<=")
        self.assertEqual(tokens[0].type, TokenType.OPERATOR)
        self.assertEqual(tokens[0].value, "<=")

    def test_greater_than_equal(self):
        """Test that >= is recognized as an operator."""
        tokens = tokenize(">=")
        self.assertEqual(tokens[0].type, TokenType.OPERATOR)
        self.assertEqual(tokens[0].value, ">=")


class TestPunctuationRecognition(unittest.TestCase):
    """Test punctuation recognition."""

    def test_open_parenthesis(self):
        """Test that ( is recognized as punctuation."""
        tokens = tokenize("(")
        self.assertEqual(tokens[0].type, TokenType.PUNCTUATION)
        self.assertEqual(tokens[0].value, "(")

    def test_close_parenthesis(self):
        """Test that ) is recognized as punctuation."""
        tokens = tokenize(")")
        self.assertEqual(tokens[0].type, TokenType.PUNCTUATION)
        self.assertEqual(tokens[0].value, ")")

    def test_comma(self):
        """Test that , is recognized as punctuation."""
        tokens = tokenize(",")
        self.assertEqual(tokens[0].type, TokenType.PUNCTUATION)
        self.assertEqual(tokens[0].value, ",")

    def test_semicolon(self):
        """Test that ; is recognized as punctuation."""
        tokens = tokenize(";")
        self.assertEqual(tokens[0].type, TokenType.PUNCTUATION)
        self.assertEqual(tokens[0].value, ";")


class TestErrorHandling(unittest.TestCase):
    """Test error handling for invalid input."""

    def test_unterminated_string(self):
        """Test that unterminated string raises SQLSyntaxError."""
        with self.assertRaises(SQLSyntaxError):
            tokenize("'unclosed")

    def test_invalid_character(self):
        """Test that invalid characters raise SQLSyntaxError."""
        with self.assertRaises(SQLSyntaxError):
            tokenize("@#$")

    def test_invalid_character_in_middle(self):
        """Test that invalid character in middle of SQL raises SQLSyntaxError."""
        with self.assertRaises(SQLSyntaxError):
            tokenize("SELECT * FROM users@table")


class TestCaseInsensitivity(unittest.TestCase):
    """Test case insensitivity for keywords."""

    def test_lowercase_create(self):
        """Test that 'create' is recognized as CREATE keyword."""
        tokens = tokenize("create")
        self.assertEqual(tokens[0].type, TokenType.KEYWORD)
        self.assertEqual(tokens[0].value, "CREATE")

    def test_mixed_case_create(self):
        """Test that 'CrEaTe' is recognized as CREATE keyword."""
        tokens = tokenize("CrEaTe")
        self.assertEqual(tokens[0].type, TokenType.KEYWORD)
        self.assertEqual(tokens[0].value, "CREATE")

    def test_lowercase_select(self):
        """Test that 'select' is recognized as SELECT keyword."""
        tokens = tokenize("select")
        self.assertEqual(tokens[0].type, TokenType.KEYWORD)
        self.assertEqual(tokens[0].value, "SELECT")


class TestComplexSQL(unittest.TestCase):
    """Test tokenization of complex SQL statements."""

    def test_create_table_statement(self):
        """Test tokenization of CREATE TABLE statement."""
        sql = "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);"
        tokens = tokenize(sql)
        token_values = [t.value for t in tokens if t.type != TokenType.EOF]

        self.assertIn("CREATE", token_values)
        self.assertIn("TABLE", token_values)
        self.assertIn("users", token_values)
        self.assertIn("id", token_values)
        self.assertIn("INTEGER", token_values)
        self.assertIn("PRIMARY", token_values)
        self.assertIn("KEY", token_values)
        self.assertIn("name", token_values)
        self.assertIn("TEXT", token_values)

    def test_insert_statement(self):
        """Test tokenization of INSERT statement."""
        sql = "INSERT INTO users (id, name) VALUES (1, 'Alice');"
        tokens = tokenize(sql)
        token_values = [t.value for t in tokens if t.type != TokenType.EOF]

        self.assertIn("INSERT", token_values)
        self.assertIn("INTO", token_values)
        self.assertIn("users", token_values)
        self.assertIn("VALUES", token_values)
        self.assertIn(1, token_values)
        self.assertIn("Alice", token_values)

    def test_select_with_where(self):
        """Test tokenization of SELECT with WHERE clause."""
        sql = "SELECT name FROM users WHERE age >= 30;"
        tokens = tokenize(sql)
        token_values = [t.value for t in tokens if t.type != TokenType.EOF]

        self.assertIn("SELECT", token_values)
        self.assertIn("name", token_values)
        self.assertIn("FROM", token_values)
        self.assertIn("users", token_values)
        self.assertIn("WHERE", token_values)
        self.assertIn("age", token_values)
        self.assertIn(">=", token_values)
        self.assertIn(30, token_values)

    def test_delete_statement(self):
        """Test tokenization of DELETE statement."""
        sql = "DELETE FROM users WHERE id = 1;"
        tokens = tokenize(sql)
        token_values = [t.value for t in tokens if t.type != TokenType.EOF]

        self.assertIn("DELETE", token_values)
        self.assertIn("FROM", token_values)
        self.assertIn("users", token_values)
        self.assertIn("WHERE", token_values)
        self.assertIn("id", token_values)
        self.assertIn("=", token_values)
        self.assertIn(1, token_values)


class TestWhitespaceHandling(unittest.TestCase):
    """Test whitespace handling."""

    def test_multiple_spaces(self):
        """Test that multiple spaces are skipped."""
        tokens = tokenize("SELECT    *")
        self.assertEqual(tokens[0].type, TokenType.KEYWORD)
        self.assertEqual(tokens[0].value, "SELECT")
        self.assertEqual(tokens[1].type, TokenType.PUNCTUATION)
        self.assertEqual(tokens[1].value, "*")

    def test_newlines(self):
        """Test that newlines are skipped."""
        tokens = tokenize("SELECT\n*\nFROM\nusers")
        token_values = [t.value for t in tokens if t.type != TokenType.EOF]
        self.assertIn("SELECT", token_values)
        self.assertIn("*", token_values)
        self.assertIn("FROM", token_values)
        self.assertIn("users", token_values)

    def test_tabs(self):
        """Test that tabs are skipped."""
        tokens = tokenize("SELECT\t*\tFROM\tusers")
        token_values = [t.value for t in tokens if t.type != TokenType.EOF]
        self.assertIn("SELECT", token_values)
        self.assertIn("*", token_values)
        self.assertIn("FROM", token_values)
        self.assertIn("users", token_values)


class TestEOFToken(unittest.TestCase):
    """Test EOF token."""

    def test_eof_token_present(self):
        """Test that EOF token is present at the end."""
        tokens = tokenize("SELECT")
        self.assertEqual(tokens[-1].type, TokenType.EOF)
        self.assertIsNone(tokens[-1].value)

    def test_empty_string(self):
        """Test that empty string produces only EOF token."""
        tokens = tokenize("")
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0].type, TokenType.EOF)