"""
SQL Parser for MiniSQLite

Parses a list of tokens into an AST.
"""

from typing import List, Optional

from minisqlite.errors import SQLSyntaxError
from minisqlite.sql.ast import (
    ASTNode,
    ColumnDef,
    Condition,
    CreateTable,
    Delete,
    Insert,
    LiteralValue,
    Select,
)
from minisqlite.sql.lexer import Token, TokenType


class Parser:
    """
    SQL parser that converts a list of tokens into an AST.

    Supports:
    - CREATE TABLE
    - INSERT INTO
    - SELECT
    - DELETE FROM
    """

    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0

    def _current(self) -> Token:
        """Return the current token."""
        if self.pos >= len(self.tokens):
            return Token(TokenType.EOF, None, -1)
        return self.tokens[self.pos]

    def _peek(self, offset: int = 1) -> Token:
        """Peek at a token ahead without advancing."""
        peek_pos = self.pos + offset
        if peek_pos >= len(self.tokens):
            return Token(TokenType.EOF, None, -1)
        return self.tokens[peek_pos]

    def _advance(self) -> Token:
        """Advance to the next token and return the current one."""
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def _expect(self, token_type: TokenType, value: Optional[str] = None) -> Token:
        """
        Expect the current token to match the given type and optionally value.
        Raise SQLSyntaxError if it doesn't match.
        """
        token = self._current()
        if token.type != token_type:
            raise SQLSyntaxError(
                f"Expected {token_type.name}, got {token.type.name} ({token.value!r}) at position {token.position}"
            )
        if value is not None and token.value != value:
            raise SQLSyntaxError(
                f"Expected {value!r}, got {token.value!r} at position {token.position}"
            )
        return self._advance()

    def _match(self, token_type: TokenType, value: Optional[str] = None) -> bool:
        """Check if the current token matches the given type and optionally value."""
        token = self._current()
        if token.type != token_type:
            return False
        if value is not None and token.value != value:
            return False
        return True

    def parse(self) -> ASTNode:
        """Parse the token stream and return the AST node."""
        if self._match(TokenType.EOF):
            raise SQLSyntaxError("Empty SQL statement")

        # Determine statement type by first keyword
        first_token = self._current()
        if first_token.type != TokenType.KEYWORD:
            raise SQLSyntaxError(
                f"Expected keyword at start of statement, got {first_token.type.name} ({first_token.value!r})"
            )

        keyword = first_token.value
        if keyword == "CREATE":
            return self._parse_create_table()
        elif keyword == "INSERT":
            return self._parse_insert()
        elif keyword == "SELECT":
            return self._parse_select()
        elif keyword == "DELETE":
            return self._parse_delete()
        else:
            raise SQLSyntaxError(f"Unknown statement type: {keyword}")

    def _parse_create_table(self) -> CreateTable:
        """Parse CREATE TABLE statement."""
        self._expect(TokenType.KEYWORD, "CREATE")
        self._expect(TokenType.KEYWORD, "TABLE")

        # Table name
        table_name_token = self._expect(TokenType.IDENTIFIER)
        table_name = table_name_token.value

        # Column definitions
        self._expect(TokenType.PUNCTUATION, "(")

        columns = []
        while not self._match(TokenType.PUNCTUATION, ")"):
            col_def = self._parse_column_def()
            columns.append(col_def)

            # Comma or closing paren
            if self._match(TokenType.PUNCTUATION, ","):
                self._advance()
            elif self._match(TokenType.PUNCTUATION, ")"):
                break
            else:
                raise SQLSyntaxError(
                    f"Expected ',' or ')' in column list, got {self._current().type.name} ({self._current().value!r})"
                )

        self._expect(TokenType.PUNCTUATION, ")")

        # Optional semicolon
        if self._match(TokenType.PUNCTUATION, ";"):
            self._advance()

        return CreateTable(table_name=table_name, columns=columns)

    def _parse_column_def(self) -> ColumnDef:
        """Parse a column definition."""
        # Column name
        name_token = self._expect(TokenType.IDENTIFIER)
        name = name_token.value

        # Column type
        type_token = self._expect(TokenType.KEYWORD)
        if type_token.value not in ("INTEGER", "TEXT"):
            raise SQLSyntaxError(
                f"Expected INTEGER or TEXT, got {type_token.value!r}"
            )
        col_type = type_token.value

        # Optional PRIMARY KEY
        primary_key = False
        if self._match(TokenType.KEYWORD, "PRIMARY"):
            self._advance()
            self._expect(TokenType.KEYWORD, "KEY")
            primary_key = True

        # Skip optional NOT NULL constraint (MVP does not enforce it, but must parse it)
        if self._match(TokenType.KEYWORD, "NOT"):
            self._advance()
            self._expect(TokenType.KEYWORD, "NULL")

        return ColumnDef(name=name, type=col_type, primary_key=primary_key)

    def _parse_insert(self) -> Insert:
        """Parse INSERT INTO statement."""
        self._expect(TokenType.KEYWORD, "INSERT")
        self._expect(TokenType.KEYWORD, "INTO")

        # Table name
        table_name_token = self._expect(TokenType.IDENTIFIER)
        table_name = table_name_token.value

        # Optional column list
        columns = []
        if self._match(TokenType.PUNCTUATION, "("):
            self._advance()
            while not self._match(TokenType.PUNCTUATION, ")"):
                col_token = self._expect(TokenType.IDENTIFIER)
                columns.append(col_token.value)

                if self._match(TokenType.PUNCTUATION, ","):
                    self._advance()
                elif self._match(TokenType.PUNCTUATION, ")"):
                    break
                else:
                    raise SQLSyntaxError(
                        f"Expected ',' or ')' in column list, got {self._current().type.name} ({self._current().value!r})"
                    )
            self._expect(TokenType.PUNCTUATION, ")")

        # VALUES keyword
        self._expect(TokenType.KEYWORD, "VALUES")

        # Value list
        self._expect(TokenType.PUNCTUATION, "(")
        values = []
        while not self._match(TokenType.PUNCTUATION, ")"):
            value = self._parse_value()
            values.append(value)

            if self._match(TokenType.PUNCTUATION, ","):
                self._advance()
            elif self._match(TokenType.PUNCTUATION, ")"):
                break
            else:
                raise SQLSyntaxError(
                    f"Expected ',' or ')' in value list, got {self._current().type.name} ({self._current().value!r})"
                )
        self._expect(TokenType.PUNCTUATION, ")")

        # Optional semicolon
        if self._match(TokenType.PUNCTUATION, ";"):
            self._advance()

        return Insert(table_name=table_name, columns=columns, values=values)

    def _parse_value(self) -> LiteralValue:
        """Parse a single value (integer or text literal)."""
        token = self._current()
        if token.type == TokenType.INTEGER:
            self._advance()
            return token.value
        elif token.type == TokenType.TEXT:
            self._advance()
            return token.value
        elif token.type == TokenType.KEYWORD and token.value == "NULL":
            # Support NULL literal as a value
            self._advance()
            return None
        else:
            raise SQLSyntaxError(
                f"Expected INTEGER, TEXT, or NULL literal, got {token.type.name} ({token.value!r})"
            )

    def _parse_select(self) -> Select:
        """Parse SELECT statement."""
        self._expect(TokenType.KEYWORD, "SELECT")

        # Column list
        columns = []
        if self._match(TokenType.PUNCTUATION, "*"):
            self._advance()
            columns = ["*"]
        else:
            while True:
                col_token = self._expect(TokenType.IDENTIFIER)
                columns.append(col_token.value)

                if self._match(TokenType.PUNCTUATION, ","):
                    self._advance()
                else:
                    break

        # FROM keyword
        self._expect(TokenType.KEYWORD, "FROM")

        # Table name
        table_name_token = self._expect(TokenType.IDENTIFIER)
        table_name = table_name_token.value

        # Optional WHERE clause
        condition = None
        if self._match(TokenType.KEYWORD, "WHERE"):
            self._advance()
            condition = self._parse_and_or_condition()

        # Optional semicolon
        if self._match(TokenType.PUNCTUATION, ";"):
            self._advance()

        return Select(columns=columns, table_name=table_name, condition=condition)

    def _parse_condition(self) -> Condition:
        """Parse a WHERE condition."""
        # Column name
        col_token = self._expect(TokenType.IDENTIFIER)
        column = col_token.value

        # Operator
        if not self._match(TokenType.OPERATOR):
            raise SQLSyntaxError(
                f"Expected operator in WHERE clause, got {self._current().type.name} ({self._current().value!r})"
            )
        operator_token = self._advance()
        operator = operator_token.value

        # Value
        value = self._parse_value()

        return Condition(column=column, operator=operator, value=value)

    def _parse_and_or_condition(self) -> Condition:
        """
        Parse a condition that may include AND/OR.
        For MVP, we only support single conditions.
        If AND or OR is encountered, raise SQLSyntaxError.
        """
        # Parse the first condition
        condition = self._parse_condition()

        # Check for AND or OR
        if self._match(TokenType.KEYWORD, "AND") or self._match(TokenType.KEYWORD, "OR"):
            raise SQLSyntaxError(
                "AND/OR operators are not supported in this MVP version"
            )

        return condition

    def _parse_delete(self) -> Delete:
        """Parse DELETE FROM statement."""
        self._expect(TokenType.KEYWORD, "DELETE")
        self._expect(TokenType.KEYWORD, "FROM")

        # Table name
        table_name_token = self._expect(TokenType.IDENTIFIER)
        table_name = table_name_token.value

        # WHERE clause (required)
        if not self._match(TokenType.KEYWORD, "WHERE"):
            raise SQLSyntaxError(
                "DELETE statement must have a WHERE clause"
            )
        self._advance()
        condition = self._parse_condition()

        # Optional semicolon
        if self._match(TokenType.PUNCTUATION, ";"):
            self._advance()

        return Delete(table_name=table_name, condition=condition)


def parse(tokens: List[Token]) -> ASTNode:
    """
    Parse a list of tokens into an AST node.

    Args:
        tokens: List of Token objects from the lexer

    Returns:
        ASTNode representing the parsed SQL statement

    Raises:
        SQLSyntaxError: If the SQL contains invalid syntax
    """
    parser = Parser(tokens)
    return parser.parse()