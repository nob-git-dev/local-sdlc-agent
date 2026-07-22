"""
SQL Lexer for MiniSQLite

Tokenizes SQL input into a stream of tokens.
"""

import re
from dataclasses import dataclass
from enum import Enum, auto
from typing import Iterator, List

from minisqlite.errors import SQLSyntaxError


class TokenType(Enum):
    """Token types for the SQL lexer."""
    KEYWORD = auto()
    IDENTIFIER = auto()
    INTEGER = auto()
    TEXT = auto()
    OPERATOR = auto()
    PUNCTUATION = auto()
    EOF = auto()


@dataclass
class Token:
    """Represents a single token in the SQL stream."""
    type: TokenType
    value: str | int
    position: int  # Character position in the original string

    def __repr__(self) -> str:
        return f"Token({self.type.name}, {self.value!r}, pos={self.position})"


# Keywords in uppercase
KEYWORDS = {
    "CREATE", "TABLE", "INSERT", "INTO", "SELECT", "FROM", "DELETE",
    "WHERE", "PRIMARY", "KEY", "VALUES", "INTEGER", "TEXT", "AND", "OR",
    "NOT", "NULL", "AS", "DISTINCT", "ORDER", "BY", "ASC", "DESC",
    "LIMIT", "OFFSET", "GROUP", "HAVING", "JOIN", "LEFT", "RIGHT",
    "INNER", "OUTER", "ON", "UNION", "ALL", "EXISTS", "BETWEEN", "LIKE",
    "IN", "CASE", "WHEN", "THEN", "ELSE", "END", "CAST", "TRUE", "FALSE"
}

# Operators
OPERATORS = ["=", "!=", "<", ">", "<=", ">=", "-"]

# Punctuation
PUNCTUATION = ["(", ")", ",", ";", "*"]


class Lexer:
    """SQL lexer that converts SQL strings into tokens."""

    def __init__(self, sql: str):
        self.sql = sql
        self.pos = 0
        self.length = len(sql)

    def _current_char(self) -> str | None:
        """Return the current character or None if at end."""
        if self.pos >= self.length:
            return None
        return self.sql[self.pos]

    def _peek_char(self, offset: int = 1) -> str | None:
        """Peek at a character ahead without advancing."""
        peek_pos = self.pos + offset
        if peek_pos >= self.length:
            return None
        return self.sql[peek_pos]

    def _advance(self, count: int = 1) -> str:
        """Advance the position and return the consumed character(s)."""
        start = self.pos
        self.pos += count
        return self.sql[start:self.pos]

    def _skip_whitespace(self) -> None:
        """Skip whitespace characters."""
        while self._current_char() and self._current_char().isspace():
            self._advance()

    def _read_identifier_or_keyword(self) -> Token:
        """Read an identifier or keyword starting from current position."""
        start_pos = self.pos
        result = []

        # First character must be letter or underscore
        char = self._current_char()
        if char and (char.isalpha() or char == "_"):
            result.append(self._advance())

        # Subsequent characters can be alphanumeric or underscore
        while True:
            char = self._current_char()
            if char and (char.isalnum() or char == "_"):
                result.append(self._advance())
            else:
                break

        value = "".join(result)
        upper_value = value.upper()

        if upper_value in KEYWORDS:
            return Token(TokenType.KEYWORD, upper_value, start_pos)
        else:
            return Token(TokenType.IDENTIFIER, value, start_pos)

    def _read_integer(self) -> Token:
        """Read an integer literal starting from current position."""
        start_pos = self.pos
        result = []

        while True:
            char = self._current_char()
            if char and char.isdigit():
                result.append(self._advance())
            else:
                break

        if not result:
            raise SQLSyntaxError(f"Expected integer at position {start_pos}")

        value = int("".join(result))
        return Token(TokenType.INTEGER, value, start_pos)

    def _read_text(self) -> Token:
        """Read a text literal starting from current position (including the opening quote)."""
        start_pos = self.pos
        self._advance()  # Skip opening quote

        result = []

        while True:
            char = self._current_char()
            if char is None:
                # Unterminated string
                raise SQLSyntaxError(f"Unterminated string literal starting at position {start_pos}")

            if char == "'":
                # Check for escaped quote
                if self._peek_char() == "'":
                    # Escaped quote: ''
                    result.append("'")
                    self._advance()  # Skip first '
                    self._advance()  # Skip second '
                else:
                    # End of string
                    self._advance()  # Skip closing quote
                    break
            else:
                result.append(self._advance())

        value = "".join(result)
        return Token(TokenType.TEXT, value, start_pos)

    def _read_operator(self) -> Token:
        """Read an operator starting from current position."""
        start_pos = self.pos
        char = self._current_char()

        if char in ["=", "!", "<", ">"]:
            # Check for two-character operators
            next_char = self._peek_char()
            if char in ["!", "<", ">"] and next_char == "=":
                self._advance(2)
                return Token(TokenType.OPERATOR, char + next_char, start_pos)

        # Single character operator (including '-')
        self._advance()
        return Token(TokenType.OPERATOR, char, start_pos)

    def _read_punctuation(self) -> Token:
        """Read punctuation starting from current position."""
        start_pos = self.pos
        char = self._current_char()

        if char in PUNCTUATION:
            self._advance()
            return Token(TokenType.PUNCTUATION, char, start_pos)

        raise SQLSyntaxError(f"Unexpected punctuation at position {start_pos}")

    def tokenize(self) -> Iterator[Token]:
        """Tokenize the entire SQL string and yield tokens."""
        while self.pos < self.length:
            self._skip_whitespace()

            if self.pos >= self.length:
                break

            char = self._current_char()
            start_pos = self.pos

            if char.isalpha() or char == "_":
                yield self._read_identifier_or_keyword()
            elif char.isdigit():
                yield self._read_integer()
            elif char == "'":
                yield self._read_text()
            elif char in ["=", "!", "<", ">"]:
                yield self._read_operator()
            elif char == "-":
                yield self._read_operator()
            elif char in PUNCTUATION:
                yield self._read_punctuation()
            else:
                raise SQLSyntaxError(f"Invalid character '{char}' at position {start_pos}")

        yield Token(TokenType.EOF, None, self.pos)


def tokenize(sql: str) -> List[Token]:
    """
    Tokenize a SQL string into a list of tokens.

    Args:
        sql: SQL string to tokenize

    Returns:
        List of Token objects

    Raises:
        SQLSyntaxError: If the SQL contains invalid syntax
    """
    lexer = Lexer(sql)
    return list(lexer.tokenize())