from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ColumnDef:
    name: str
    type_name: str
    primary_key: bool = False


@dataclass(frozen=True)
class Literal:
    value: object
    type_name: str


@dataclass(frozen=True)
class Condition:
    column: str
    operator: str
    value: Literal


@dataclass(frozen=True)
class CreateTable:
    table_name: str
    columns: list[ColumnDef]


@dataclass(frozen=True)
class Insert:
    table_name: str
    columns: Optional[list[str]]
    values: list[Literal]


@dataclass(frozen=True)
class Select:
    table_name: str
    columns: Optional[list[str]]
    where: Optional[Condition] = None


@dataclass(frozen=True)
class Delete:
    table_name: str
    where: Condition
