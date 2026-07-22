"""Connection layer for Mini SQLite Engine.

Provides connect(), execute() for CREATE TABLE, INSERT, SELECT, DELETE,
and close().
Schema metadata is persisted in page 1 as JSON.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Optional

from minisqlite.errors import (
    DuplicateKeyError,
    SchemaError,
    SQLSyntaxError,
    StorageError,
    TypeMismatchError,
)
from minisqlite.sql.ast import (
    ColumnDef,
    Condition,
    CreateTable,
    Delete,
    Insert,
    Literal,
    Select,
)
from minisqlite.sql.parser import parse
from minisqlite.storage.btree import BTree
from minisqlite.storage.pager import Pager
from minisqlite.storage.record import encode_record


PAGE_SIZE = 4096
HEADER_SIZE = 100
SCHEMA_PAGE_ID = 1


@dataclass
class Result:
    """Result of an execute() call."""

    columns: list[str] = field(default_factory=list)
    rows: list[list[object]] = field(default_factory=list)
    rows_affected: int = 0


class Connection:
    """A connection to a Mini SQLite database file."""

    def __init__(self, path: str):
        self._path = path
        self._pager = Pager(path)
        if self._pager._new_db:
            # Reserve page 1 for the schema catalog before any BTree
            # allocation can claim it.  Allocate page 1 explicitly so
            # that subsequent BTree(self._pager) calls allocate from
            # page 2 onward.  Then write an empty schema so the
            # catalog is valid on disk.
            self._pager.allocate_page()  # reserves page 1
            self._schema = {"tables": {}}
            self._save_schema()
        else:
            self._schema = self._load_schema()

    def _load_schema(self) -> dict:
        """Load schema catalog from page 1.

        Returns an empty schema dict if the database is new or page 1
        contains no valid JSON.
        """
        if self._pager._new_db:
            return {"tables": {}}

        try:
            raw = self._pager.read_page(SCHEMA_PAGE_ID)
            # Strip null bytes to get the JSON string
            text = raw.rstrip(b"\x00").decode("utf-8")
            if not text:
                return {"tables": {}}
            data = json.loads(text)
            if not isinstance(data, dict) or "tables" not in data:
                return {"tables": {}}
            return data
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {"tables": {}}

    def _save_schema(self) -> None:
        """Persist the current schema catalog to page 1."""
        text = json.dumps(self._schema, ensure_ascii=False)
        data = text.encode("utf-8")
        if len(data) > PAGE_SIZE:
            raise StorageError(
                f"Schema too large for page: {len(data)} > {PAGE_SIZE}"
            )
        self._pager.write_page(SCHEMA_PAGE_ID, data)

    def _get_table_schema(self, table_name: str) -> dict:
        """Get the schema entry for a table. Raises SchemaError if not found."""
        if table_name not in self._schema["tables"]:
            raise SchemaError(f"Unknown table: '{table_name}'")
        return self._schema["tables"][table_name]

    def _get_btree(self, table_name: str) -> BTree:
        """Open a BTree for the given table."""
        schema = self._get_table_schema(table_name)
        return BTree(self._pager, root_page_id=schema["root_page_id"])

    def _validate_value_type(self, value, type_name: str) -> None:
        """Validate that a Python value matches the declared column type."""
        if value is None:
            return
        if type_name == "INTEGER":
            if not isinstance(value, int):
                raise TypeMismatchError(
                    f"Expected INTEGER for column, got {type(value).__name__}"
                )
        elif type_name == "TEXT":
            if not isinstance(value, str):
                raise TypeMismatchError(
                    f"Expected TEXT for column, got {type(value).__name__}"
                )

    def execute(self, sql: str) -> Result:
        """Execute a SQL statement.

        Supports CREATE TABLE, INSERT, SELECT, DELETE.
        """
        ast = parse(sql)

        if isinstance(ast, CreateTable):
            return self._execute_create_table(ast)
        elif isinstance(ast, Insert):
            return self._execute_insert(ast)
        elif isinstance(ast, Select):
            return self._execute_select(ast)
        elif isinstance(ast, Delete):
            return self._execute_delete(ast)
        else:
            raise NotImplementedError(
                f"Statement type {type(ast).__name__} is not yet implemented"
            )

    def _execute_create_table(self, ast: CreateTable) -> Result:
        """Handle CREATE TABLE statement."""
        table_name = ast.table_name

        if table_name in self._schema["tables"]:
            raise SchemaError(
                f"Table '{table_name}' already exists"
            )

        # Allocate a new page for the table's BTree root
        # Initialize the BTree with no root_page_id so it creates and initializes a fresh leaf root
        btree = BTree(self._pager)
        root_page_id = btree.root_page_id

        # Build column metadata
        columns_meta = []
        for col in ast.columns:
            columns_meta.append(
                {
                    "name": col.name,
                    "type": col.type_name,
                    "primary_key": col.primary_key,
                }
            )

        # Store schema entry
        self._schema["tables"][table_name] = {
            "columns": columns_meta,
            "root_page_id": root_page_id,
            "next_rowid": 1,
        }

        # Persist schema to page 1
        self._save_schema()

        return Result(columns=[], rows=[], rows_affected=0)

    def _execute_insert(self, ast: Insert) -> Result:
        """Handle INSERT statement."""
        table_name = ast.table_name
        schema = self._get_table_schema(table_name)
        columns_meta = schema["columns"]

        # Determine which columns are being inserted
        if ast.columns is not None:
            # Explicit column list
            col_names = ast.columns
        else:
            # All columns
            col_names = [c["name"] for c in columns_meta]

        # Validate column names
        col_meta_map = {c["name"]: c for c in columns_meta}
        for col_name in col_names:
            if col_name not in col_meta_map:
                raise SchemaError(f"Unknown column: '{col_name}'")

        # Build value map
        values = ast.values
        if len(values) != len(col_names):
            raise SQLSyntaxError(
                f"Expected {len(col_names)} values, got {len(values)}"
            )

        value_map = {}
        for col_name, literal in zip(col_names, values):
            col_meta = col_meta_map[col_name]
            self._validate_value_type(literal.value, col_meta["type"])
            value_map[col_name] = literal.value

        # Determine rowid
        rowid = None
        for col_meta in columns_meta:
            if col_meta["primary_key"]:
                if col_meta["name"] in value_map:
                    rowid = value_map[col_meta["name"]]
                else:
                    # Auto-generate rowid
                    rowid = schema["next_rowid"]
                    schema["next_rowid"] += 1
                break

        if rowid is None:
            # No primary key defined, auto-generate
            rowid = schema["next_rowid"]
            schema["next_rowid"] += 1

        # Build the full row values in schema order
        row_values = []
        for col_meta in columns_meta:
            if col_meta["name"] in value_map:
                row_values.append(value_map[col_meta["name"]])
            elif col_meta["primary_key"]:
                row_values.append(rowid)
            else:
                row_values.append(None)

        # Encode record
        payload = encode_record(row_values)

        # Insert into BTree
        btree = self._get_btree(table_name)
        btree.insert(rowid, payload)

        # Update schema
        self._save_schema()

        return Result(columns=[], rows=[], rows_affected=1)

    def _execute_select(self, ast: Select) -> Result:
        """Handle SELECT statement."""
        table_name = ast.table_name
        schema = self._get_table_schema(table_name)
        columns_meta = schema["columns"]

        # Determine which columns to return
        if ast.columns is None:
            # SELECT *
            result_columns = [c["name"] for c in columns_meta]
        else:
            # Validate column names
            result_columns = []
            for col_name in ast.columns:
                found = False
                for c in columns_meta:
                    if c["name"] == col_name:
                        result_columns.append(col_name)
                        found = True
                        break
                if not found:
                    raise SchemaError(f"Unknown column: '{col_name}'")

        # Get all rows from BTree
        btree = self._get_btree(table_name)
        all_rows = btree.scan_all()

        # Apply WHERE clause if present
        filtered_rows = []
        for rowid, payload in all_rows:
            row_values = self._decode_row(payload, columns_meta)
            if ast.where is not None:
                if not self._evaluate_where(row_values, columns_meta, ast.where):
                    continue
            filtered_rows.append((rowid, row_values))

        # Build result
        columns = result_columns
        rows = []
        for rowid, row_values in filtered_rows:
            row = []
            for col_name in columns:
                idx = next(i for i, c in enumerate(columns_meta) if c["name"] == col_name)
                row.append(row_values[idx])
            rows.append(row)

        return Result(columns=columns, rows=rows, rows_affected=0)

    def _execute_delete(self, ast: Delete) -> Result:
        """Handle DELETE statement."""
        table_name = ast.table_name
        schema = self._get_table_schema(table_name)
        columns_meta = schema["columns"]

        # Get all rows from BTree
        btree = self._get_btree(table_name)
        all_rows = btree.scan_all()

        # Find rows to delete
        rows_to_delete = []
        for rowid, payload in all_rows:
            row_values = self._decode_row(payload, columns_meta)
            if self._evaluate_where(row_values, columns_meta, ast.where):
                rows_to_delete.append(rowid)

        # Delete rows
        for rowid in rows_to_delete:
            btree.delete(rowid)

        return Result(columns=[], rows=[], rows_affected=len(rows_to_delete))

    def _decode_row(self, payload: bytes, columns_meta: list[dict]) -> list:
        """Decode a row payload into a list of values."""
        from minisqlite.storage.record import decode_record
        return decode_record(payload)

    def _evaluate_where(self, row_values: list, columns_meta: list[dict], condition: Condition) -> bool:
        """Evaluate a WHERE condition against a row."""
        # Find the column index
        col_idx = None
        for i, c in enumerate(columns_meta):
            if c["name"] == condition.column:
                col_idx = i
                break

        if col_idx is None:
            raise SchemaError(f"Unknown column in WHERE: '{condition.column}'")

        # Get the column value
        col_value = row_values[col_idx]

        # Compare
        if condition.operator == "=":
            return col_value == condition.value.value
        elif condition.operator == "!=":
            return col_value != condition.value.value
        elif condition.operator == ">":
            return col_value > condition.value.value
        elif condition.operator == ">=":
            return col_value >= condition.value.value
        elif condition.operator == "<":
            return col_value < condition.value.value
        elif condition.operator == "<=":
            return col_value <= condition.value.value
        else:
            raise SQLSyntaxError(f"Unsupported operator: '{condition.operator}'")

    def list_tables(self) -> list[str]:
        """List all table names in the database."""
        return list(self._schema["tables"].keys())

    def get_schema(self, table_name: str) -> str:
        """Get the CREATE TABLE statement for a table.

        Raises SchemaError if the table does not exist.
        """
        schema = self._get_table_schema(table_name)
        columns = schema["columns"]

        # Build column definitions
        col_defs = []
        for col in columns:
            parts = [col["name"], col["type"]]
            if col["primary_key"]:
                parts.append("PRIMARY KEY")
            col_defs.append(" ".join(parts))

        return f"CREATE TABLE {table_name} ({', '.join(col_defs)})"

    def close(self) -> None:
        """Close the connection and persist schema."""
        self._pager.close()


def connect(path: str) -> Connection:
    """Open a connection to a database file at the given path."""
    return Connection(path)