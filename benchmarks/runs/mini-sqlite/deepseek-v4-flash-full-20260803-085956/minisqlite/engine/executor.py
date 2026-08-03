"""SQL Executor for MiniSQLite Engine.

This module implements the Executor component described in SPEC.md
section 10.1.  It translates parsed AST statements into storage
operations on the B+Tree and schema registry.

Responsibilities:
- CREATE TABLE: validate columns/primary key, create a B+Tree,
  register schema metadata, return an empty Result.
- INSERT: map column names to table columns, validate types,
  auto-assign rowid, insert into the B+Tree, sync the root page,
  return a Result with rows_affected=1.
- SELECT/DELETE handlers are appended in a later stage; this stage
  only dispatches to them.
"""

from minisqlite.errors import SchemaError, TypeMismatchError
from minisqlite.result import Result
from minisqlite.sql.ast import (
    ColumnDef,
    CreateTable,
    Delete,
    Insert,
    IntegerLiteral,
    Literal,
    NullLiteral,
    Select,
    TextLiteral,
)
from minisqlite.engine.schema import ColumnMetadata
from minisqlite.storage.btree import BTree
from minisqlite.storage.record import decode_record, encode_record


class Executor:
    """Execute parsed SQL statements against the storage layer."""

    def __init__(self, schema_registry, pager):
        """Store the schema registry and pager for later use."""
        self._schema = schema_registry
        self._pager = pager

    def execute(self, statement, sql_text=""):
        """Dispatch a parsed statement to the matching handler.

        Returns a Result for CREATE TABLE and INSERT.  SELECT and
        DELETE handlers are appended in a later stage; this stage
        only dispatches to them.
        """
        if isinstance(statement, CreateTable):
            return self._execute_create(statement, sql_text)
        if isinstance(statement, Insert):
            return self._execute_insert(statement)
        if isinstance(statement, Select):
            return self._execute_select(statement)
        if isinstance(statement, Delete):
            return self._execute_delete(statement)
        raise SchemaError("unsupported statement type")

    def _execute_create(self, statement, sql_text=""):
        """Create a table: validate columns, create a B+Tree, register schema.

        Returns Result([], [], 0) on success.
        """
        table_name = statement.table_name
        if self._schema.get_table(table_name) is not None:
            raise SchemaError("table %r already exists" % table_name)
        if not statement.columns:
            raise SchemaError("table %r must have at least one column" % table_name)

        columns = []
        primary_key_columns = []
        for column_def in statement.columns:
            column_type = column_def.column_type.upper()
            if column_type not in ("INTEGER", "TEXT"):
                raise SchemaError(
                    "column %r has unsupported type %r"
                    % (column_def.name, column_def.column_type)
                )
            columns.append(
                ColumnMetadata(column_def.name, column_type, column_def.primary_key)
            )
            if column_def.primary_key:
                primary_key_columns.append(column_def.name)

        if len(primary_key_columns) > 1:
            raise SchemaError("composite primary keys are not supported")
        if len(primary_key_columns) == 1:
            pk_name = primary_key_columns[0]
            pk_type = None
            for column_def in statement.columns:
                if column_def.name == pk_name:
                    pk_type = column_def.column_type.upper()
                    break
            if pk_type != "INTEGER":
                raise SchemaError(
                    "primary key column %r must be INTEGER" % pk_name
                )
            rowid_column = pk_name
        else:
            rowid_column = None

        tree = BTree(self._pager)
        self._schema.create_table(
            table_name,
            columns,
            rowid_column,
            tree.root_page_id,
            sql_text,
        )
        return Result([], [], 0)

    def _execute_insert(self, statement):
        """Insert a row into a table.

        Maps column names to table columns, validates types, assigns a
        rowid, inserts into the B+Tree, and syncs the root page.
        Returns Result([], [], 1) on success.
        """
        table = self._require_table(statement.table_name)
        tree = self._open_btree(table)

        column_names = statement.column_names
        if column_names:
            if len(column_names) != len(statement.values):
                raise SchemaError(
                    "column count %d does not match value count %d"
                    % (len(column_names), len(statement.values))
                )
            for name in column_names:
                if not any(c.name == name for c in table.columns):
                    raise SchemaError(
                        "table %r has no column %r"
                        % (table.name, name)
                    )
        else:
            column_names = [c.name for c in table.columns]
            if len(column_names) != len(statement.values):
                raise SchemaError(
                    "column count %d does not match value count %d"
                    % (len(column_names), len(statement.values))
                )

        values_by_name = {}
        for name, literal in zip(column_names, statement.values):
            values_by_name[name] = self._literal_value(literal)

        row_values = []
        rowid = None
        for column in table.columns:
            if column.name in values_by_name:
                value = values_by_name[column.name]
            else:
                value = None
            self._check_type(column, value)
            if table.rowid_column is not None and column.name == table.rowid_column:
                if value is None:
                    raise TypeMismatchError(
                        "primary key column %r cannot be NULL" % column.name
                    )
                rowid = value
            row_values.append(value)

        if rowid is None:
            rowid = self._next_rowid(tree)

        payload = encode_record(row_values)
        tree.insert(rowid, payload)
        self._sync_root(table, tree)
        return Result([], [], 1)

    def _require_table(self, table_name):
        """Return TableMetadata for table_name or raise SchemaError."""
        table = self._schema.get_table(table_name)
        if table is None:
            raise SchemaError("table %r does not exist" % table_name)
        return table

    def _open_btree(self, table):
        """Open the B+Tree for a table using its stored root page id."""
        return BTree(self._pager, table.root_page_id)

    def _btree_scan(self, tree):
        """Return all (rowid, payload) pairs from the tree in rowid order."""
        return tree.scan_all()

    def _sync_root(self, table, tree):
        """Persist the tree's current root page id into the schema registry."""
        self._schema.update_root_page(table.name, tree.root_page_id)

    def _literal_value(self, literal):
        """Convert an AST literal to a Python value."""
        if isinstance(literal, IntegerLiteral):
            return literal.value
        if isinstance(literal, TextLiteral):
            return literal.value
        if isinstance(literal, NullLiteral):
            return None
        raise SchemaError("unsupported literal type")

    def _check_type(self, column, value):
        """Validate that value matches the column's declared type.

        Raises TypeMismatchError when the value type does not match.
        NULL is always accepted.
        """
        if value is None:
            return
        if column.type == "INTEGER":
            if not isinstance(value, int):
                raise TypeMismatchError(
                    "column %r expects INTEGER, got %r" % (column.name, value)
                )
        elif column.type == "TEXT":
            if not isinstance(value, str):
                raise TypeMismatchError(
                    "column %r expects TEXT, got %r" % (column.name, value)
                )
        else:
            raise SchemaError("column %r has unknown type %r" % (column.name, column.type))

    def _next_rowid(self, tree):
        """Return the next rowid for a table without an INTEGER PRIMARY KEY.

        Scans all rows and returns max(rowid) + 1, or 1 for an empty table.
        """
        max_rowid = 0
        for rowid, _payload in self._btree_scan(tree):
            if rowid > max_rowid:
                max_rowid = rowid
        return max_rowid + 1

    def _execute_select(self, statement):
        """Execute a SELECT statement.

        Validates the table and columns, projects columns in the
        requested order (or all columns for SELECT *), applies a single
        WHERE condition, scans the B+Tree in rowid order, decodes each
        record, and returns a Result with columns and rows.
        """
        table = self._require_table(statement.table_name)
        tree = self._open_btree(table)

        if statement.column_names == ["*"]:
            column_names = [c.name for c in table.columns]
        else:
            column_names = list(statement.column_names)
            for name in column_names:
                if not any(c.name == name for c in table.columns):
                    raise SchemaError(
                        "table %r has no column %r" % (table.name, name)
                    )

        rows = []
        for rowid, payload in self._btree_scan(tree):
            values = decode_record(payload)
            if len(values) != len(table.columns):
                raise SchemaError(
                    "record column count %d does not match table %r"
                    % (len(values), table.name)
                )
            row = []
            for name in column_names:
                index = None
                for i, column in enumerate(table.columns):
                    if column.name == name:
                        index = i
                        break
                if index is None:
                    raise SchemaError(
                        "table %r has no column %r" % (table.name, name)
                    )
                row.append(values[index])
            if statement.condition is not None:
                if not self._condition_matches(statement.condition, table, values):
                    continue
            rows.append(row)

        return Result(column_names, rows, len(rows))

    def _execute_delete(self, statement):
        """Execute a DELETE statement.

        Collects matching rowids first, then deletes each row from the
        B+Tree, syncs the root page, and returns rows_affected.
        """
        table = self._require_table(statement.table_name)
        tree = self._open_btree(table)

        matching_rowids = []
        for rowid, payload in self._btree_scan(tree):
            values = decode_record(payload)
            if len(values) != len(table.columns):
                raise SchemaError(
                    "record column count %d does not match table %r"
                    % (len(values), table.name)
                )
            if self._condition_matches(statement.condition, table, values):
                matching_rowids.append(rowid)

        for rowid in matching_rowids:
            tree.delete(rowid)
        self._sync_root(table, tree)
        return Result([], [], len(matching_rowids))

    def _column_index(self, table, column_name):
        """Return the index of column_name in table.columns or raise SchemaError."""
        for i, column in enumerate(table.columns):
            if column.name == column_name:
                return i
        raise SchemaError(
            "table %r has no column %r" % (table.name, column_name)
        )

    def _condition_matches(self, condition, table, values):
        """Evaluate a single WHERE condition against decoded row values.

        Raises SchemaError for an unknown column and TypeMismatchError
        when the literal type does not match the column type.
        """
        index = self._column_index(table, condition.column_name)
        column = table.columns[index]
        value = values[index]
        literal_value = self._literal_value(condition.literal)
        self._check_type(column, literal_value)

        if condition.operator == "=":
            return value == literal_value
        if condition.operator == "!=":
            return value != literal_value
        if condition.operator == "<":
            return value < literal_value
        if condition.operator == "<=":
            return value <= literal_value
        if condition.operator == ">":
            return value > literal_value
        if condition.operator == ">=":
            return value >= literal_value
        raise SchemaError("unsupported operator %r" % condition.operator)