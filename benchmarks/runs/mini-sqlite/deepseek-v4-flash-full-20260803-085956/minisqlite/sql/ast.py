# minisqlite/sql/ast.py
"""AST node definitions for the MiniSQLite Engine.

This module defines the data structures that represent parsed SQL
statements. It contains no execution logic; it only describes the
syntax tree for CREATE TABLE, INSERT, SELECT, DELETE, and a single
WHERE condition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Union


class Literal:
    """Base class for literal values in expressions."""


@dataclass(frozen=True)
class IntegerLiteral(Literal):
    """An INTEGER literal value."""

    value: int

    @property
    def type_name(self) -> str:
        return "INTEGER"


@dataclass(frozen=True)
class TextLiteral(Literal):
    """A TEXT literal value."""

    value: str

    @property
    def type_name(self) -> str:
        return "TEXT"


@dataclass(frozen=True)
class NullLiteral(Literal):
    """A NULL literal value."""

    value: None = None

    @property
    def type_name(self) -> str:
        return "NULL"


@dataclass(frozen=True)
class ColumnDef:
    """Definition of a single column in a CREATE TABLE statement."""

    name: str
    column_type: str
    primary_key: bool = False

    @property
    def type_name(self) -> str:
        return self.column_type


@dataclass(frozen=True)
class Condition:
    """A single WHERE condition: column_name <op> literal."""

    column_name: str
    operator: str
    literal: Literal

    @property
    def column(self) -> str:
        return self.column_name

    @property
    def value(self) -> Literal:
        return self.literal


@dataclass(frozen=True)
class CreateTable:
    """A CREATE TABLE statement."""

    table_name: str
    columns: List[ColumnDef]


@dataclass(frozen=True)
class Insert:
    """An INSERT INTO statement.

    If column_names is empty, the statement used the column-list-less
    form and values correspond to all columns in table order.
    """

    table_name: str
    column_names: List[str]
    values: List[Literal]

    @property
    def columns(self) -> Optional[List[str]]:
        """Return column_names, or None when the column list is empty."""
        return self.column_names if self.column_names else None


@dataclass(frozen=True)
class Select:
    """A SELECT statement.

    column_names is ["*"] for SELECT * FROM table.
    condition is None when there is no WHERE clause.
    """

    column_names: List[str]
    table_name: str
    condition: Optional[Condition] = None

    @property
    def columns(self) -> Optional[List[str]]:
        """Return column_names, or None when selecting all columns."""
        return self.column_names if self.column_names != ["*"] else None

    @property
    def where(self) -> Optional[Condition]:
        """Return the WHERE condition."""
        return self.condition


@dataclass(frozen=True)
class Delete:
    """A DELETE statement.

    condition is required; DELETE without WHERE is rejected by the
    parser per the MVP safety constraint.
    """

    table_name: str
    condition: Condition

    @property
    def where(self) -> Condition:
        """Return the WHERE condition."""
        return self.condition


Statement = Union[CreateTable, Insert, Select, Delete]