"""SQL Lexer for Mini SQLite Engine.

Tokenizes SQL strings into a sequence of Token objects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, auto
from typing import List

from minisqlite.errors import SQLSyntaxError


class TokenType(Enum):
    """Token types produced by the lexer."""

    KEYWORD = auto()
    IDENTIFIER = auto()
    INTEGER = auto()
    STRING = auto()
    SYMBOL = auto()
    OPERATOR = auto()
    EOF = auto()


# Keywords recognized by the lexer (case-insensitive).
KEYWORDS: frozenset[str] = frozenset(
    {
        "CREATE",
        "TABLE",
        "INTEGER",
        "TEXT",
        "PRIMARY",
        "KEY",
        "INSERT",
        "INTO",
        "VALUES",
        "SELECT",
        "FROM",
        "WHERE",
        "DELETE",
    }
)

# Multi-character operators (must be checked before single-character symbols).
OPERATORS: tuple[str, ...] = ("!=", ">=", "<=", "=", ">", "<")

# Single-character symbols.
SYMBOLS: frozenset[str] = frozenset({"(", ")", ",", ";", "*"})

# Identifier pattern: starts with letter or underscore, followed by
# letters, digits, or underscores.
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Integer literal pattern: optional leading minus followed by digits.
_INTEGER_RE = re.compile(r"-?\d+")


@dataclass(frozen=True)
class Token:
    """A single lexical token."""

    type: TokenType
    value: str


def _classify_token(value: str) -> TokenType:
    """Return the TokenType for a given token value."""
    upper = value.upper()
    if upper in KEYWORDS:
        return TokenType.KEYWORD
    if _IDENTIFIER_RE.fullmatch(value) and not value.lstrip("-").isdigit():
        return TokenType.IDENTIFIER
    if _INTEGER_RE.fullmatch(value):
        return TokenType.INTEGER
    return TokenType.IDENTIFIER


def tokenize(sql: str) -> List[Token]:
    """Tokenize a SQL string into a list of Token objects.

    Raises:
        SQLSyntaxError: If the input contains an unterminated string
            literal or an unexpected character.
    """
    tokens: List[Token] = []
    i = 0
    length = len(sql)

    while i < length:
        ch = sql[i]

        # Whitespace is skipped.
        if ch.isspace():
            i += 1
            continue

        # String literal.
        if ch == "'":
            i += 1
            parts: List[str] = []
            while i < length:
                c = sql[i]
                if c == "'":
                    # Check for escaped quote ''.
                    if i + 1 < length and sql[i + 1] == "'":
                        parts.append("'")
                        i += 2
                        continue
                    # End of string literal.
                    i += 1
                    break
                parts.append(c)
                i += 1
            else:
                raise SQLSyntaxError("Unterminated string literal")
            tokens.append(Token(TokenType.STRING, "".join(parts)))
            continue

        # Multi-character operators.
        matched_op = False
        for op in OPERATORS:
            if sql[i : i + len(op)] == op:
                tokens.append(Token(TokenType.OPERATOR, op))
                i += len(op)
                matched_op = True
                break
        if matched_op:
            continue

        # Single-character symbols.
        if ch in SYMBOLS:
            tokens.append(Token(TokenType.SYMBOL, ch))
            i += 1
            continue

        # Identifier or keyword or integer literal.
        m = _IDENTIFIER_RE.match(sql, i)
        if m:
            value = m.group(0)
            token_type = _classify_token(value)
            tokens.append(Token(token_type, value))
            i += len(value)
            continue

        # Integer literal (may start with minus).
        if ch == "-" and i + 1 < length and sql[i + 1].isdigit():
            m = _INTEGER_RE.match(sql, i)
            if m:
                tokens.append(Token(TokenType.INTEGER, m.group(0)))
                i += len(m.group(0))
                continue

        # Digit-only integer literal.
        if ch.isdigit():
            m = _INTEGER_RE.match(sql, i)
            if m:
                tokens.append(Token(TokenType.INTEGER, m.group(0)))
                i += len(m.group(0))
                continue

        raise SQLSyntaxError(f"Unexpected character: {ch!r}")

    tokens.append(Token(TokenType.EOF, ""))
    return tokens
