"""SQL parser for MiniSQLite Engine.

Consumes the token stream produced by sql/lexer.py and builds AST
nodes defined in sql/ast.py. Raises SQLSyntaxError for invalid syntax.
"""

from typing import List, Optional, Tuple

from minisqlite.errors import SQLSyntaxError
from minisqlite.sql.ast import (
    ColumnDef,
    Condition,
    CreateTable,
    Delete,
    Insert,
    IntegerLiteral,
    Literal,
    Select,
    Statement,
    TextLiteral,
)
from minisqlite.sql.lexer import (
    IDENTIFIER,
    INTEGER,
    KEYWORD,
    OP,
    SEMICOLON,
    STRING,
    SYMBOL,
    Token,
    tokenize,
)

# Comparison operators accepted in WHERE conditions
COMPARISON_OPERATORS = {"=", "!=", ">", ">=", "<", "<="}

# Column types accepted in CREATE TABLE
COLUMN_TYPES = {"INTEGER", "TEXT"}


class _Parser:
    """Internal recursive-descent parser over a token list."""

    def __init__(self, tokens: List[Token]) -> None:
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> Optional[Token]:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def advance(self) -> Token:
        token = self.peek()
        if token is None:
            raise SQLSyntaxError("unexpected end of input")
        self.pos += 1
        return token

    def expect_keyword(self, keyword: str) -> None:
        token = self.advance()
        if token[0] != KEYWORD or token[1] != keyword:
            raise SQLSyntaxError(f"expected keyword {keyword}, got {token[1]!r}")

    def expect_symbol(self, symbol: str) -> None:
        token = self.advance()
        if token[0] != SYMBOL or token[1] != symbol:
            raise SQLSyntaxError(f"expected symbol {symbol}, got {token[1]!r}")

    def expect_identifier(self) -> str:
        token = self.advance()
        if token[0] != IDENTIFIER:
            raise SQLSyntaxError(f"expected identifier, got {token[1]!r}")
        return token[1]

    def expect_operator(self) -> str:
        token = self.advance()
        if token[0] != OP or token[1] not in COMPARISON_OPERATORS:
            raise SQLSyntaxError(f"expected comparison operator, got {token[1]!r}")
        return token[1]

    def expect_literal(self) -> Literal:
        token = self.advance()
        kind, value = token
        if kind == INTEGER:
            return IntegerLiteral(int(value))
        if kind == STRING:
            return TextLiteral(value)
        raise SQLSyntaxError(f"expected literal, got {value!r}")

    def parse(self) -> Statement:
        if self.peek() is None:
            raise SQLSyntaxError("empty statement")
        first = self.peek()
        if first[0] != KEYWORD:
            raise SQLSyntaxError(f"expected SQL keyword, got {first[1]!r}")
        keyword = first[1]
        if keyword == "CREATE":
            return self.parse_create()
        if keyword == "INSERT":
            return self.parse_insert()
        if keyword == "SELECT":
            return self.parse_select()
        if keyword == "DELETE":
            return self.parse_delete()
        raise SQLSyntaxError(f"unsupported statement keyword: {keyword}")

    def parse_create(self) -> CreateTable:
        self.expect_keyword("CREATE")
        self.expect_keyword("TABLE")
        table_name = self.expect_identifier()
        self.expect_symbol("(")
        columns: List[ColumnDef] = []
        while True:
            column_name = self.expect_identifier()
            type_token = self.advance()
            if type_token[0] != KEYWORD or type_token[1] not in COLUMN_TYPES:
                raise SQLSyntaxError(f"expected column type, got {type_token[1]!r}")
            column_type = type_token[1]
            primary_key = False
            if self.peek() is not None and self.peek()[0] == KEYWORD and self.peek()[1] == "PRIMARY":
                self.advance()
                self.expect_keyword("KEY")
                primary_key = True
            columns.append(ColumnDef(column_name, column_type, primary_key))
            if self.peek() is not None and self.peek()[0] == SYMBOL and self.peek()[1] == ",":
                self.advance()
                continue
            break
        self.expect_symbol(")")
        self._consume_optional_semicolon()
        self._expect_end()
        return CreateTable(table_name, columns)

    def parse_insert(self) -> Insert:
        self.expect_keyword("INSERT")
        self.expect_keyword("INTO")
        table_name = self.expect_identifier()
        column_names: List[str] = []
        if self.peek() is not None and self.peek()[0] == SYMBOL and self.peek()[1] == "(":
            self.advance()
            while True:
                column_names.append(self.expect_identifier())
                if self.peek() is not None and self.peek()[0] == SYMBOL and self.peek()[1] == ",":
                    self.advance()
                    continue
                break
            self.expect_symbol(")")
        self.expect_keyword("VALUES")
        self.expect_symbol("(")
        values: List[Literal] = []
        while True:
            values.append(self.expect_literal())
            if self.peek() is not None and self.peek()[0] == SYMBOL and self.peek()[1] == ",":
                self.advance()
                continue
            break
        self.expect_symbol(")")
        self._consume_optional_semicolon()
        self._expect_end()
        return Insert(table_name, column_names, values)

    def parse_select(self) -> Select:
        self.expect_keyword("SELECT")
        column_names: List[str] = []
        if self.peek() is not None and self.peek()[0] == SYMBOL and self.peek()[1] == "*":
            self.advance()
            column_names.append("*")
        else:
            while True:
                column_names.append(self.expect_identifier())
                if self.peek() is not None and self.peek()[0] == SYMBOL and self.peek()[1] == ",":
                    self.advance()
                    continue
                break
        self.expect_keyword("FROM")
        table_name = self.expect_identifier()
        condition: Optional[Condition] = None
        if self.peek() is not None and self.peek()[0] == KEYWORD and self.peek()[1] == "WHERE":
            self.advance()
            condition = self.parse_condition()
        self._consume_optional_semicolon()
        self._expect_end()
        return Select(column_names, table_name, condition)

    def parse_delete(self) -> Delete:
        self.expect_keyword("DELETE")
        self.expect_keyword("FROM")
        table_name = self.expect_identifier()
        if self.peek() is None or self.peek()[0] != KEYWORD or self.peek()[1] != "WHERE":
            raise SQLSyntaxError("DELETE requires a WHERE clause")
        self.advance()
        condition = self.parse_condition()
        self._consume_optional_semicolon()
        self._expect_end()
        return Delete(table_name, condition)

    def parse_condition(self) -> Condition:
        column_name = self.expect_identifier()
        operator = self.expect_operator()
        literal = self.expect_literal()
        return Condition(column_name, operator, literal)

    def _consume_optional_semicolon(self) -> None:
        if self.peek() is None:
            raise SQLSyntaxError("expected semicolon, got end of input")
        if self.peek()[0] != SEMICOLON:
            raise SQLSyntaxError(f"expected semicolon, got {self.peek()[1]!r}")
        self.advance()

    def _expect_end(self) -> None:
        if self.peek() is not None:
            token = self.peek()
            raise SQLSyntaxError(f"unexpected token: {token[1]!r}")


def parse(sql: str) -> Statement:
    """Parse SQL text into an AST node.

    Raises SQLSyntaxError for invalid syntax.
    """
    tokens = tokenize(sql)
    return _Parser(tokens).parse()