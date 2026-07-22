"""SQL parser for Mini SQLite Engine."""

from __future__ import annotations

from typing import List, Optional

from minisqlite.errors import SQLSyntaxError
from minisqlite.sql.ast import (
    ColumnDef,
    Condition,
    CreateTable,
    Delete,
    Insert,
    Literal,
    Select,
)
from minisqlite.sql.lexer import Token, TokenType, tokenize


class Parser:
    """Recursive descent parser for Mini SQLite SQL statements."""

    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0

    def parse(self) -> object:
        """Parse the token stream and return an AST node."""
        stmt = self._parse_statement()
        if not self._check(TokenType.EOF):
            raise SQLSyntaxError(
                f"Unexpected token: {self._current().value!r}"
            )
        return stmt

    def _current(self) -> Token:
        if self.pos >= len(self.tokens):
            raise SQLSyntaxError("Unexpected end of input")
        return self.tokens[self.pos]

    def _advance(self) -> Token:
        token = self._current()
        self.pos += 1
        return token

    def _check(self, token_type: TokenType) -> bool:
        return self._current().type == token_type

    def _check_keyword(self, keyword: str) -> bool:
        token = self._current()
        return (
            token.type == TokenType.KEYWORD
            and token.value.upper() == keyword.upper()
        )

    def _expect(self, token_type: TokenType) -> Token:
        if self._check(token_type):
            return self._advance()
        raise SQLSyntaxError(
            f"Expected {token_type.name}, got {self._current().type.name}"
        )

    def _expect_keyword(self, keyword: str) -> Token:
        if self._check_keyword(keyword):
            return self._advance()
        raise SQLSyntaxError(
            f"Expected keyword '{keyword}', got '{self._current().value}'"
        )

    def _parse_statement(self) -> object:
        if self._check_keyword("CREATE"):
            return self._parse_create_table()
        elif self._check_keyword("INSERT"):
            return self._parse_insert()
        elif self._check_keyword("SELECT"):
            return self._parse_select()
        elif self._check_keyword("DELETE"):
            return self._parse_delete()
        else:
            raise SQLSyntaxError(
                f"Unexpected token: {self._current().value!r}"
            )

    def _parse_create_table(self) -> CreateTable:
        self._expect_keyword("CREATE")
        self._expect_keyword("TABLE")
        table_name = self._expect(TokenType.IDENTIFIER).value
        self._expect(TokenType.SYMBOL)  # (
        columns = self._parse_column_defs()
        self._expect(TokenType.SYMBOL)  # )
        self._expect(TokenType.SYMBOL)  # ;
        return CreateTable(table_name=table_name, columns=columns)

    def _parse_column_defs(self) -> List[ColumnDef]:
        columns = []
        columns.append(self._parse_column_def())
        while self._check(TokenType.SYMBOL):
            if self._current().value == ",":
                self._advance()
                columns.append(self._parse_column_def())
            else:
                break
        return columns

    def _parse_column_def(self) -> ColumnDef:
        name = self._expect(TokenType.IDENTIFIER).value
        type_name = self._parse_column_type()
        primary_key = False
        if self._check_keyword("PRIMARY"):
            self._advance()
            self._expect_keyword("KEY")
            primary_key = True
        return ColumnDef(name=name, type_name=type_name, primary_key=primary_key)

    def _parse_column_type(self) -> str:
        if self._check_keyword("INTEGER"):
            self._advance()
            return "INTEGER"
        elif self._check_keyword("TEXT"):
            self._advance()
            return "TEXT"
        else:
            raise SQLSyntaxError(
                f"Expected column type 'INTEGER' or 'TEXT', got '{self._current().value}'"
            )

    def _parse_insert(self) -> Insert:
        self._expect_keyword("INSERT")
        self._expect_keyword("INTO")
        table_name = self._expect(TokenType.IDENTIFIER).value

        columns: Optional[List[str]] = None
        if self._check(TokenType.SYMBOL) and self._current().value == "(":
            self._advance()
            columns = self._parse_identifier_list()
            self._expect(TokenType.SYMBOL)  # )

        self._expect_keyword("VALUES")
        self._expect(TokenType.SYMBOL)  # (
        values = self._parse_value_list()
        self._expect(TokenType.SYMBOL)  # )
        self._expect(TokenType.SYMBOL)  # ;
        return Insert(table_name=table_name, columns=columns, values=values)

    def _parse_identifier_list(self) -> List[str]:
        names = [self._expect(TokenType.IDENTIFIER).value]
        while self._check(TokenType.SYMBOL) and self._current().value == ",":
            self._advance()
            names.append(self._expect(TokenType.IDENTIFIER).value)
        return names

    def _parse_value_list(self) -> List[Literal]:
        values = [self._parse_value()]
        while self._check(TokenType.SYMBOL) and self._current().value == ",":
            self._advance()
            values.append(self._parse_value())
        return values

    def _parse_value(self) -> Literal:
        if self._check(TokenType.INTEGER):
            token = self._advance()
            return Literal(value=int(token.value), type_name="INTEGER")
        elif self._check(TokenType.STRING):
            token = self._advance()
            return Literal(value=token.value, type_name="TEXT")
        else:
            raise SQLSyntaxError(
                f"Expected literal value, got {self._current().value!r}"
            )

    def _parse_select(self) -> Select:
        self._expect_keyword("SELECT")

        columns: Optional[List[str]] = None
        if self._check(TokenType.SYMBOL) and self._current().value == "*":
            self._advance()
        else:
            columns = self._parse_identifier_list()

        self._expect_keyword("FROM")
        table_name = self._expect(TokenType.IDENTIFIER).value

        where: Optional[Condition] = None
        if self._check_keyword("WHERE"):
            self._advance()
            where = self._parse_condition()

        self._expect(TokenType.SYMBOL)  # ;
        return Select(table_name=table_name, columns=columns, where=where)

    def _parse_condition(self) -> Condition:
        column = self._expect(TokenType.IDENTIFIER).value
        operator = self._parse_operator()
        value = self._parse_value()
        return Condition(column=column, operator=operator, value=value)

    def _parse_operator(self) -> str:
        token = self._current()
        if token.type == TokenType.OPERATOR:
            self._advance()
            return token.value
        elif token.type == TokenType.SYMBOL and token.value in ("=", "!=", ">=", "<=", ">", "<"):
            self._advance()
            return token.value
        else:
            raise SQLSyntaxError(
                f"Expected operator, got {token.value!r}"
            )

    def _parse_delete(self) -> Delete:
        self._expect_keyword("DELETE")
        self._expect_keyword("FROM")
        table_name = self._expect(TokenType.IDENTIFIER).value

        if not self._check_keyword("WHERE"):
            raise SQLSyntaxError("DELETE requires WHERE clause")

        self._advance()
        where = self._parse_condition()
        self._expect(TokenType.SYMBOL)  # ;
        return Delete(table_name=table_name, where=where)


def parse(sql: str) -> object:
    """Parse a SQL string and return an AST node."""
    tokens = tokenize(sql)
    parser = Parser(tokens)
    return parser.parse()
