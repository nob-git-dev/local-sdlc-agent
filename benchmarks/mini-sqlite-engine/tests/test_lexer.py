"""Tests for the Mini SQLite Engine SQL lexer."""

import unittest

from minisqlite.errors import SQLSyntaxError
from minisqlite.sql.lexer import Token, TokenType, tokenize


class TestLexerKeywords(unittest.TestCase):
    """Keywords are recognized case-insensitively."""

    def test_keywords_case_insensitive(self):
        sql = "create table INTEGER text PRIMARY KEY insert into values select from where delete"
        tokens = tokenize(sql)
        # Strip the trailing EOF token for comparison.
        keyword_tokens = [t for t in tokens if t.type == TokenType.KEYWORD]
        expected = [
            "create", "table", "INTEGER", "text", "PRIMARY", "KEY",
            "insert", "into", "values", "select", "from", "where", "delete",
        ]
        self.assertEqual([t.value for t in keyword_tokens], expected)


class TestLexerIdentifiers(unittest.TestCase):
    """Identifiers are recognized."""

    def test_identifiers(self):
        tokens = tokenize("foo bar_baz _qux")
        id_tokens = [t for t in tokens if t.type == TokenType.IDENTIFIER]
        self.assertEqual([t.value for t in id_tokens], ["foo", "bar_baz", "_qux"])


class TestLexerIntegers(unittest.TestCase):
    """Integer literals are recognized."""

    def test_integer_literals(self):
        tokens = tokenize("123 -10 0")
        int_tokens = [t for t in tokens if t.type == TokenType.INTEGER]
        self.assertEqual([t.value for t in int_tokens], ["123", "-10", "0"])


class TestLexerStrings(unittest.TestCase):
    """Text (string) literals are recognized."""

    def test_string_literals(self):
        tokens = tokenize("'hello' 'world'")
        str_tokens = [t for t in tokens if t.type == TokenType.STRING]
        self.assertEqual([t.value for t in str_tokens], ["hello", "world"])

    def test_escaped_quote(self):
        """It''s OK decodes to It's OK."""
        tokens = tokenize("'It''s OK'")
        str_tokens = [t for t in tokens if t.type == TokenType.STRING]
        self.assertEqual(len(str_tokens), 1)
        self.assertEqual(str_tokens[0].value, "It's OK")


class TestLexerOperators(unittest.TestCase):
    """Operators are recognized."""

    def test_operators(self):
        tokens = tokenize("= != > >= < <=")
        op_tokens = [t for t in tokens if t.type == TokenType.OPERATOR]
        self.assertEqual(
            [t.value for t in op_tokens],
            ["=", "!=", ">", ">=", "<", "<="],
        )

    def test_symbols(self):
        tokens = tokenize("( ) , ; *")
        sym_tokens = [t for t in tokens if t.type == TokenType.SYMBOL]
        self.assertEqual(
            [t.value for t in sym_tokens],
            ["(", ")", ",", ";", "*"],
        )


class TestLexerErrors(unittest.TestCase):
    """Lexer errors raise SQLSyntaxError."""

    def test_unterminated_string(self):
        with self.assertRaises(SQLSyntaxError):
            tokenize("'unterminated")

    def test_unexpected_character(self):
        with self.assertRaises(SQLSyntaxError):
            tokenize("SELECT * FROM t WHERE x @ y")


if __name__ == "__main__":
    unittest.main()
