"""Schema Registry for MiniSQLite Engine.

This module implements the schema metadata lifecycle described in
SPEC.md section 10.1.  Page 1 is reserved as the dedicated schema
metadata page.  Table metadata is serialized as JSON and zero-padded
to exactly PAGE_SIZE bytes.  Table data itself lives in B+Tree pages,
never in the schema JSON.
"""

import json

from minisqlite.errors import CorruptDatabaseError, SchemaError
from minisqlite.storage.file_format import PAGE_SIZE


class ColumnMetadata:
    """Per-column metadata: name, type, and primary_key flag."""

    def __init__(self, name, column_type, primary_key=False):
        self.name = name
        self.type = column_type
        self.primary_key = primary_key

    def to_dict(self):
        return {
            "name": self.name,
            "type": self.type,
            "primary_key": self.primary_key,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(data["name"], data["type"], data["primary_key"])


class TableMetadata:
    """Per-table metadata: name, columns, rowid_column, root_page_id, sql."""

    def __init__(self, name, columns, rowid_column, root_page_id, sql):
        self.name = name
        self.columns = columns
        self.rowid_column = rowid_column
        self.root_page_id = root_page_id
        self.sql = sql

    def to_dict(self):
        return {
            "name": self.name,
            "columns": [c.to_dict() for c in self.columns],
            "rowid_column": self.rowid_column,
            "root_page_id": self.root_page_id,
            "sql": self.sql,
        }

    @classmethod
    def from_dict(cls, data):
        columns = [ColumnMetadata.from_dict(c) for c in data["columns"]]
        return cls(
            data["name"],
            columns,
            data["rowid_column"],
            data["root_page_id"],
            data["sql"],
        )


class SchemaRegistry:
    """Own table metadata and persist it to the dedicated schema page.

    The schema metadata page is page 1, the first data page.  It is
    reserved at open and re-read on reopen.  The schema JSON contains
    only table metadata and is zero-padded to exactly PAGE_SIZE bytes.
    """

    def __init__(self, pager):
        self._pager = pager
        self._schema_page = 1
        self._tables = {}
        self._load()

    def _load(self):
        """Read the schema metadata page and populate the tables dict."""
        try:
            raw = self._pager.read_page(self._schema_page)
        except CorruptDatabaseError:
            # A fresh database has no schema page yet; reserve page 1 now.
            page_id = self._pager.allocate_page()
            if page_id != self._schema_page:
                raise CorruptDatabaseError(
                    "expected schema page %d, got %d"
                    % (self._schema_page, page_id)
                )
            self.save()
            return
        if len(raw) != PAGE_SIZE:
            raise CorruptDatabaseError(
                "schema page must be exactly %d bytes, got %d"
                % (PAGE_SIZE, len(raw))
            )
        # Strip trailing zero padding before parsing JSON.
        payload = raw.rstrip(b"\x00")
        if not payload:
            self._tables = {}
            return
        try:
            data = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise CorruptDatabaseError("schema page contains invalid JSON")
        if not isinstance(data, dict):
            raise CorruptDatabaseError("schema page JSON must be an object")
        if "tables" not in data:
            raise CorruptDatabaseError(
                "schema page JSON must contain a 'tables' key"
            )
        tables_data = data["tables"]
        if not isinstance(tables_data, dict):
            raise CorruptDatabaseError(
                "schema page 'tables' must be an object"
            )
        tables = {}
        for name, table_data in tables_data.items():
            try:
                tables[name] = TableMetadata.from_dict(table_data)
            except (KeyError, TypeError):
                raise CorruptDatabaseError(
                    "schema page contains invalid table metadata"
                )
        self._tables = tables

    def create_table(self, name, columns, rowid_column, root_page_id, sql):
        """Register a new table and persist the schema.

        Raises SchemaError if the table name already exists or the
        metadata is invalid.
        """
        if not name:
            raise SchemaError("table name must be non-empty")
        if name in self._tables:
            raise SchemaError("table %r already exists" % name)
        if not columns:
            raise SchemaError("table %r must have at least one column" % name)
        if not rowid_column:
            raise SchemaError("table %r must have a rowid_column" % name)
        if root_page_id < 0:
            raise SchemaError("root_page_id must be non-negative")
        if not sql:
            raise SchemaError("table %r must have a sql definition" % name)
        self._tables[name] = TableMetadata(
            name, columns, rowid_column, root_page_id, sql
        )
        self.save()

    def get_table(self, name):
        """Return TableMetadata for the given name, or None if missing."""
        return self._tables.get(name)

    def update_root_page(self, name, root_page_id):
        """Update the stored root_page_id for a table and persist."""
        table = self._tables.get(name)
        if table is None:
            raise SchemaError("table %r does not exist" % name)
        if root_page_id < 0:
            raise SchemaError("root_page_id must be non-negative")
        table.root_page_id = root_page_id
        self.save()

    def table_names(self):
        """Return the list of registered table names."""
        return list(self._tables.keys())

    def save(self):
        """Serialize table metadata to the schema page and flush the Pager.

        Raises SchemaError or CorruptDatabaseError if the serialized
        metadata exceeds PAGE_SIZE bytes.
        """
        payload = {}
        for name, table in self._tables.items():
            payload[name] = table.to_dict()
        data = json.dumps({"tables": payload}).encode("utf-8")
        if len(data) > PAGE_SIZE:
            raise SchemaError(
                "schema metadata exceeds %d bytes" % PAGE_SIZE
            )
        padded = data + b"\x00" * (PAGE_SIZE - len(data))
        self._pager.write_page(self._schema_page, padded)
        self._pager.flush()