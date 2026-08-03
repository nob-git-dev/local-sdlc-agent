"""Unit tests for the SQL lexer.

Covers keyword, identifier, integer, and string tokenization,
doubled single-quote escaping (It''s OK), and unterminated
string literal error handling. Uses only the public API exposed
by minisqlite.sql.lexer.
"""

import unittest

from minisqlite.sql.lexer import (
    IDENTIFIER,
    INTEGER,
    KEYWORD,
    OP,
    SEMICOLON,
    STRING,
    SYMBOL,
    tokenize,
)
from minisqlite.errors import SQLSyntaxError


class LexerKeywordTests(unittest.TestCase):
    def test_keywords_are_recognized(self):
        tokens = tokenize("CREATE TABLE")
        self.assertEqual(tokens[0], (KEYWORD, "CREATE"))
        self.assertEqual(tokens[1], (KEYWORD, "TABLE"))

    def test_keywords_are_case_insensitive(self):
        tokens = tokenize("select")
        self.assertEqual(tokens[0], (KEYWORD, "SELECT"))


class LexerIdentifierTests(unittest.TestCase):
    def test_identifier_is_recognized(self):
        tokens = tokenize("users")
        self.assertEqual(tokens[0], (IDENTIFIER, "users"))

    def test_identifier_with_underscore_and_digits(self):
        tokens = tokenize("user_2")
        self.assertEqual(tokens[0], (IDENTIFIER, "user_2"))


class LexerIntegerTests(unittest.TestCase):
    def test_positive_integer(self):
        tokens = tokenize("123")
        self.assertEqual(tokens[0], (INTEGER, "123"))

    def test_negative_integer(self):
        tokens = tokenize("-10")
        self.assertEqual(tokens[0], (INTEGER, "-10"))

    def test_zero(self):
        tokens = tokenize("0")
        self.assertEqual(tokens[0], (INTEGER, "0"))


class LexerStringTests(unittest.TestCase):
    def test_string_literal(self):
        tokens = tokenize("'hello'")
        self.assertEqual(tokens[0], (STRING, "hello"))

    def test_string_with_doubled_quote_escape(self):
        tokens = tokenize("'It''s OK'")
        self.assertEqual(tokens[0], (STRING, "It's OK"))

    def test_string_with_escaped_quote_in_middle(self):
        tokens = tokenize("'a''b'")
        self.assertEqual(tokens[0], (STRING, "a'b"))

    def test_unterminated_string_raises_sql_syntax_error(self):
        with self.assertRaises(SQLSyntaxError):
            tokenize("'abc")


class LexerSymbolTests(unittest.TestCase):
    def test_symbols_are_recognized(self):
        tokens = tokenize("( , ) * .")
        self.assertEqual(tokens[0], (SYMBOL, "("))
        self.assertEqual(tokens[1], (SYMBOL, ","))
        self.assertEqual(tokens[2], (SYMBOL, ")"))
        self.assertEqual(tokens[3], (SYMBOL, "*"))
        self.assertEqual(tokens[4], (SYMBOL, "."))

    def test_semicolon_is_recognized(self):
        tokens = tokenize(";")
        self.assertEqual(tokens[0], (SEMICOLON, ";"))

    def test_comparison_operators_are_recognized(self):
        tokens = tokenize("= != > >= < <=")
        self.assertEqual(tokens[0], (OP, "="))
        self.assertEqual(tokens[1], (OP, "!="))
        self.assertEqual(tokens[2], (OP, ">"))
        self.assertEqual(tokens[3], (OP, ">="))
        self.assertEqual(tokens[4], (OP, "<"))
        self.assertEqual(tokens[5], (OP, "<="))


class LexerCombinedTests(unittest.TestCase):
    def test_full_insert_statement_tokenizes(self):
        tokens = tokenize("INSERT INTO users (id, name) VALUES (1, 'Alice');")
        kinds = [kind for kind, _ in tokens]
        self.assertEqual(kinds[0], KEYWORD)
        self.assertEqual(kinds[1], KEYWORD)
        self.assertEqual(kinds[2], IDENTIFIER)
        self.assertEqual(kinds[3], SYMBOL)
        self.assertEqual(kinds[4], IDENTIFIER)
        self.assertEqual(kinds[5], SYMBOL)
        self.assertEqual(kinds[6], IDENTIFIER)
        self.assertEqual(kinds[7], SYMBOL)
        self.assertEqual(kinds[8], KEYWORD)
        self.assertEqual(kinds[9], SYMBOL)
        self.assertEqual(kinds[10], INTEGER)
        self.assertEqual(kinds[11], SYMBOL)
        self.assertEqual(kinds[12], STRING)
        self.assertEqual(kinds[13], SYMBOL)
        self.assertEqual(kinds[14], SEMICOLON)


if __name__ == "__main__":
    unittest.main()