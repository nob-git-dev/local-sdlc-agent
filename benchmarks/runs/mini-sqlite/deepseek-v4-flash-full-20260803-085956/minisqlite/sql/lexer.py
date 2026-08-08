"""SQL lexer for MiniSQLite Engine.

Converts SQL text into a list of tokens. Handles keywords,
identifiers, integers, single-quoted strings with doubled
single-quote escaping, symbols, comparison operators, and
semicolons. An unterminated string literal raises SQLSyntaxError.
"""

from __future__ import annotations

from typing import List, Tuple

# TokenType namespace: public token kinds.
# Keeps existing string constants unchanged; OP and SEMICOLON are
# mapped to OPERATOR and SYMBOL by the Token class.
class TokenType:
    KEYWORD = "KEYWORD"
    IDENTIFIER = "IDENTIFIER"
    INTEGER = "INTEGER"
    STRING = "STRING"
    OPERATOR = "OPERATOR"
    SYMBOL = "SYMBOL"

from ..errors import SQLSyntaxError

# Token kinds
KEYWORD = "KEYWORD"
IDENTIFIER = "IDENTIFIER"
INTEGER = "INTEGER"
STRING = "STRING"
SYMBOL = "SYMBOL"
OP = "OP"
SEMICOLON = "SEMICOLON"

# SQL keywords (case-insensitive)
KEYWORDS = {
    "CREATE",
    "TABLE",
    "INSERT",
    "INTO",
    "VALUES",
    "SELECT",
    "FROM",
    "WHERE",
    "DELETE",
    "PRIMARY",
    "KEY",
    "INTEGER",
    "TEXT",
}

# Single-character symbols
SYMBOLS = set("(),*.")

# Comparison operators and other multi-character operators
OPERATORS = {
    "=",
    "!=",
    ">",
    ">=",
    "<",
    "<=",
}

# Token class: supports tuple comparison, unpacking, and indexing
# while also exposing .type and .value. OP is mapped to OPERATOR and
# SEMICOLON to SYMBOL so callers can rely on the public TokenType names.
class Token:
    __slots__ = ("kind", "_normalized_value", "value")

    def __init__(self, kind: str, normalized_value: str, public_value: str | None = None):
        self.kind = kind
        self._normalized_value = normalized_value
        self.value = public_value if public_value is not None else normalized_value

    @property
    def type(self) -> str:
        if self.kind == "OP":
            return TokenType.OPERATOR
        if self.kind == "SEMICOLON":
            return TokenType.SYMBOL
        return self.kind

    def __iter__(self):
        yield self.kind
        yield self._normalized_value

    def __getitem__(self, index: int) -> str:
        if index == 0:
            return self.kind
        if index == 1:
            return self._normalized_value
        raise IndexError(index)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Token):
            return self.kind == other.kind and self._normalized_value == other._normalized_value
        if isinstance(other, tuple):
            return (self.kind, self._normalized_value) == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.kind, self._normalized_value))

    def __repr__(self) -> str:
        return f"Token({self.kind!r}, {self._normalized_value!r})"


def tokenize(sql: str) -> List[Token]:
    """Convert SQL text into a list of (kind, value) tokens.

    Raises SQLSyntaxError for an unterminated string literal.
    """
    tokens: List[Token] = []
    i = 0
    n = len(sql)

    while i < n:
        ch = sql[i]

        # Skip whitespace
        if ch.isspace():
            i += 1
            continue

        # Semicolon
        if ch == ";":
            tokens.append(Token(SEMICOLON, ";"))
            i += 1
            continue

        # Single-quoted string literal
        if ch == "'":
            token, i = _scan_string(sql, i)
            tokens.append(token)
            continue

        # Identifier or keyword
        if ch.isalpha() or ch == "_":
            token, i = _scan_identifier(sql, i)
            tokens.append(token)
            continue

        # Integer literal
        if ch.isdigit() or (ch == "-" and i + 1 < n and sql[i + 1].isdigit()):
            token, i = _scan_integer(sql, i)
            tokens.append(token)
            continue

        # Comparison operators
        if ch in ("=", "!", ">", "<"):
            token, i = _scan_operator(sql, i)
            tokens.append(token)
            continue

        # Symbols
        if ch in SYMBOLS:
            tokens.append(Token(SYMBOL, ch))
            i += 1
            continue

        # Unknown character
        raise SQLSyntaxError(f"unexpected character: {ch!r}")

    return tokens


def _scan_identifier(sql: str, start: int) -> Tuple[Token, int]:
    """Scan an identifier or keyword starting at start."""
    i = start
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch.isalnum() or ch == "_":
            i += 1
        else:
            break
    value = sql[start:i]
    if value.upper() in KEYWORDS:
        return Token(KEYWORD, value.upper(), value), i
    return Token(IDENTIFIER, value), i


def _scan_integer(sql: str, start: int) -> Tuple[Token, int]:
    """Scan an integer literal starting at start (may include leading '-').

    Raises SQLSyntaxError if the integer is malformed.
    """
    i = start
    n = len(sql)
    if sql[i] == "-":
        i += 1
    if i >= n or not sql[i].isdigit():
        raise SQLSyntaxError("invalid integer literal")
    while i < n and sql[i].isdigit():
        i += 1
    value = sql[start:i]
    return Token(INTEGER, value), i


def _scan_string(sql: str, start: int) -> Tuple[Token, int]:
    """Scan a single-quoted string literal starting at start.

    Doubled single quotes ('') represent a literal single quote.
    Raises SQLSyntaxError if the string is not terminated.
    """
    i = start + 1  # skip opening quote
    n = len(sql)
    chars: List[str] = []

    while i < n:
        ch = sql[i]
        if ch == "'":
            # Check for doubled quote (escaped single quote)
            if i + 1 < n and sql[i + 1] == "'":
                chars.append("'")
                i += 2
                continue
            # Closing quote
            i += 1
            return Token(STRING, "".join(chars)), i
        chars.append(ch)
        i += 1

    raise SQLSyntaxError("unterminated string literal")


def _scan_operator(sql: str, start: int) -> Tuple[Token, int]:
    """Scan a comparison operator starting at start."""
    i = start
    n = len(sql)
    ch = sql[i]

    if ch == "!":
        if i + 1 < n and sql[i + 1] == "=":
            return Token(OP, "!="), i + 2
        raise SQLSyntaxError("unexpected character: '!'")
    if ch == ">":
        if i + 1 < n and sql[i + 1] == "=":
            return Token(OP, ">="), i + 2
        return Token(OP, ">"), i + 1
    if ch == "<":
        if i + 1 < n and sql[i + 1] == "=":
            return Token(OP, "<="), i + 2
        return Token(OP, "<"), i + 1
    if ch == "=":
        return Token(OP, "="), i + 1

    raise SQLSyntaxError(f"unexpected character: {ch!r}")