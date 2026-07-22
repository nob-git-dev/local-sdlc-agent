"""
SQL Execution Engine for MiniSQLite

Executes parsed AST statements against the storage engine.
"""

from typing import Any, Dict, List, Optional

from minisqlite.errors import (
    SchemaError,
    TypeMismatchError,
    DuplicateKeyError,
    SQLSyntaxError,
)
from minisqlite.result import Result
from minisqlite.sql.ast import (
    CreateTable,
    Insert,
    Select,
    Delete,
    Condition,
    LiteralValue,
)
from minisqlite.engine.schema import Schema, ColumnDef, TableDef
from minisqlite.storage.btree import BPlusTree
from minisqlite.storage.record import RecordCodec


class Executor:
    """
    Executes SQL statements.

    Coordinates between the schema, storage, and result generation.
    """

    def __init__(self, schema: Schema):
        self.schema = schema
        self._btrees: Dict[str, BPlusTree] = {}  # Cache for B+Tree instances

    def _get_btree(self, table_name: str) -> BPlusTree:
        """Get or create a B+Tree for a table."""
        if table_name not in self._btrees:
            table_def = self.schema.get_table(table_name)
            self._btrees[table_name] = BPlusTree(
                self.schema.pager, table_def.root_page_id
            )
        return self._btrees[table_name]

    def execute(self, statement: Any) -> Result:
        """
        Execute an AST statement and return a Result.

        Args:
            statement: An AST node (CreateTable, Insert, Select, Delete)

        Returns:
            Result object with columns, rows, and rowcount
        """
        if isinstance(statement, CreateTable):
            return self._execute_create_table(statement)
        elif isinstance(statement, Insert):
            return self._execute_insert(statement)
        elif isinstance(statement, Select):
            return self._execute_select(statement)
        elif isinstance(statement, Delete):
            return self._execute_delete(statement)
        else:
            raise SQLSyntaxError(f"Unknown statement type: {type(statement)}")

    def _execute_create_table(self, stmt: CreateTable) -> Result:
        """Execute CREATE TABLE statement."""
        # Convert AST ColumnDef to internal ColumnDef
        columns = []
        for col in stmt.columns:
            columns.append(
                ColumnDef(name=col.name, col_type=col.type, is_primary_key=col.primary_key)
            )

        # Create the table in schema (this allocates a root page)
        self.schema.create_table(stmt.table_name, columns)

        return Result(rowcount=0)

    def _execute_insert(self, stmt: Insert) -> Result:
        """Execute INSERT INTO statement."""
        table_name = stmt.table_name

        # Check table exists
        if not self.schema.table_exists(table_name):
            raise SchemaError(f"Table '{table_name}' not found")

        table_def = self.schema.get_table(table_name)

        # Handle column list
        if not stmt.columns:
            # Insert into all columns in order
            target_columns = [col.name for col in table_def.columns]
        else:
            target_columns = stmt.columns

        # Validate column count
        if len(target_columns) != len(stmt.values):
            raise TypeMismatchError(
                f"Expected {len(target_columns)} values, got {len(stmt.values)}"
            )

        # Validate types
        self.schema.validate_row(table_name, stmt.values)

        # Build row data as a dict
        row_data = {}
        for col_name, value in zip(target_columns, stmt.values):
            row_data[col_name] = value

        # Handle rowid
        table_def = self.schema.get_table(table_name)
        rowid_column = table_def.rowid_column

        if rowid_column:
            # Explicit rowid column - extract value from row_data
            if rowid_column not in row_data:
                raise SchemaError(f"Value required for PRIMARY KEY column '{rowid_column}'")
            rowid = row_data[rowid_column]
            # Ensure rowid is an integer for B+Tree insert
            if not isinstance(rowid, int):
                raise TypeMismatchError(f"PRIMARY KEY column '{rowid_column}' must be INTEGER, got {type(rowid).__name__}")
        else:
            # Auto-increment rowid - let B+Tree handle it by passing None
            # But B+Tree expects an integer, so we need to handle auto-increment here
            # Get the next available rowid
            max_rowid = 0
            for existing_rowid, _ in self._get_btree(table_name).scan_all():
                if existing_rowid > max_rowid:
                    max_rowid = existing_rowid
            rowid = max_rowid + 1

        # Get B+Tree and insert
        btree = self._get_btree(table_name)

        # Encode the row
        codec = RecordCodec()
        # Order values by column definition order
        ordered_values = []
        for col in table_def.columns:
            if col.name in row_data:
                ordered_values.append(row_data[col.name])
            else:
                # This shouldn't happen due to validation above
                raise SchemaError(f"Missing value for column '{col.name}'")

        payload = codec.encode(ordered_values)

        try:
            inserted_rowid = btree.insert(rowid, payload)
        except ValueError as e:
            if "duplicate" in str(e).lower():
                raise DuplicateKeyError(str(e))
            raise

        return Result(rowcount=1)

    def _execute_select(self, stmt: Select) -> Result:
        """Execute SELECT statement."""
        table_name = stmt.table_name

        # Check table exists
        if not self.schema.table_exists(table_name):
            raise SchemaError(f"Table '{table_name}' not found")

        table_def = self.schema.get_table(table_name)

        # Determine output columns
        if stmt.columns == ["*"]:
            output_columns = [col.name for col in table_def.columns]
        else:
            output_columns = stmt.columns
            # Validate columns exist
            for col_name in output_columns:
                try:
                    table_def.get_column_index(col_name)
                except SchemaError:
                    raise SchemaError(f"Column '{col_name}' not found in table '{table_name}'")

        # Get B+Tree
        btree = self._get_btree(table_name)
        codec = RecordCodec()

        # Scan all rows
        rows = []
        for rowid, payload in btree.scan_all():
            # Decode the row
            values = codec.decode(payload)

            # Apply WHERE condition if present
            if stmt.condition:
                if not self._evaluate_condition(values, table_def, stmt.condition):
                    continue

            # Build output row
            output_row = []
            for col_name in output_columns:
                col_idx = table_def.get_column_index(col_name)
                output_row.append(values[col_idx])

            rows.append(output_row)

        return Result(columns=output_columns, rows=rows, rowcount=len(rows))

    def _execute_delete(self, stmt: Delete) -> Result:
        """Execute DELETE FROM statement."""
        # Check WHERE clause is present
        if stmt.condition is None:
            raise SQLSyntaxError(
                "DELETE without WHERE clause is not allowed. "
                "Use 'DELETE FROM table WHERE rowid >= 0' to delete all rows."
            )

        table_name = stmt.table_name

        # Check table exists
        if not self.schema.table_exists(table_name):
            raise SchemaError(f"Table '{table_name}' not found")

        table_def = self.schema.get_table(table_name)
        btree = self._get_btree(table_name)
        codec = RecordCodec()

        # Find rows to delete
        deleted_count = 0
        rows_to_delete = []

        for rowid, payload in btree.scan_all():
            values = codec.decode(payload)
            if self._evaluate_condition(values, table_def, stmt.condition):
                rows_to_delete.append(rowid)

        # Delete the rows
        for rowid in rows_to_delete:
            btree.delete(rowid)
            deleted_count += 1

        return Result(rowcount=deleted_count)

    def _evaluate_condition(
        self, values: List, table_def: TableDef, condition: Condition
    ) -> bool:
        """Evaluate a WHERE condition against a row."""
        try:
            col_idx = table_def.get_column_index(condition.column)
        except SchemaError:
            raise SchemaError(f"Column '{condition.column}' not found in table '{table_def.name}'")

        actual_value = values[col_idx]
        expected_value = condition.value

        # Type checking for comparison
        if isinstance(actual_value, int) and isinstance(expected_value, int):
            # Integer comparison
            pass
        elif isinstance(actual_value, str) and isinstance(expected_value, str):
            # String comparison
            pass
        else:
            # Type mismatch - condition doesn't match
            return False

        # Evaluate the operator
        if condition.operator == "=":
            return actual_value == expected_value
        elif condition.operator == "!=":
            return actual_value != expected_value
        elif condition.operator == "<":
            return actual_value < expected_value
        elif condition.operator == ">":
            return actual_value > expected_value
        elif condition.operator == "<=":
            return actual_value <= expected_value
        elif condition.operator == ">=":
            return actual_value >= expected_value
        else:
            raise SQLSyntaxError(f"Unknown operator: {condition.operator}")