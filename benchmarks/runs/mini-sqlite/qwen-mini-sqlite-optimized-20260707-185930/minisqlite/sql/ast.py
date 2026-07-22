"""
Abstract Syntax Tree (AST) for MiniSQLite

Defines AST node classes for SQL statements.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Union


# Value types that can appear in SQL
LiteralValue = Union[int, str, None]


class ASTNode(ABC):
    """Base class for all AST nodes."""

    @abstractmethod
    def __repr__(self) -> str:
        """Return a string representation of the node."""
        pass


@dataclass
class ColumnDef(ASTNode):
    """Column definition in CREATE TABLE."""
    name: str
    type: str  # 'INTEGER' or 'TEXT'
    primary_key: bool = False

    def __repr__(self) -> str:
        pk_str = " PRIMARY KEY" if self.primary_key else ""
        return f"ColumnDef(name={self.name!r}, type={self.type!r}{pk_str})"


@dataclass
class Condition(ASTNode):
    """WHERE clause condition."""
    column: str
    operator: str  # =, !=, <, >, <=, >=
    value: LiteralValue

    def __repr__(self) -> str:
        return f"Condition(column={self.column!r}, operator={self.operator!r}, value={self.value!r})"


@dataclass
class CreateTable(ASTNode):
    """CREATE TABLE statement."""
    table_name: str
    columns: List[ColumnDef] = field(default_factory=list)

    def __repr__(self) -> str:
        cols = ", ".join(str(c) for c in self.columns)
        return f"CreateTable(table_name={self.table_name!r}, columns=[{cols}])"


@dataclass
class Insert(ASTNode):
    """INSERT INTO statement."""
    table_name: str
    columns: List[str] = field(default_factory=list)  # Empty means all columns
    values: List[LiteralValue] = field(default_factory=list)

    def __repr__(self) -> str:
        cols = ", ".join(self.columns) if self.columns else "*"
        vals = ", ".join(repr(v) for v in self.values)
        return f"Insert(table_name={self.table_name!r}, columns=[{cols}], values=[{vals}])"


@dataclass
class Select(ASTNode):
    """SELECT statement."""
    columns: List[str]  # Empty or ["*"] means all columns
    table_name: str
    condition: Optional[Condition] = None

    def __repr__(self) -> str:
        cols = ", ".join(self.columns) if self.columns else "*"
        cond_str = f" WHERE {self.condition}" if self.condition else ""
        return f"Select(columns=[{cols}], table_name={self.table_name!r}{cond_str})"


@dataclass
class Delete(ASTNode):
    """DELETE FROM statement."""
    table_name: str
    condition: Condition

    def __repr__(self) -> str:
        return f"Delete(table_name={self.table_name!r}, condition={self.condition})"


# Type alias for any SQL statement
SQLStatement = Union[CreateTable, Insert, Select, Delete]