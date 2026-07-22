"""
Schema Management for MiniSQLite

Handles table definitions, column metadata, and schema persistence.
"""

from typing import Dict, List, Optional, Tuple

from minisqlite.errors import SchemaError, CorruptDatabaseError
from minisqlite.storage.pager import Pager
from minisqlite.storage.record import RecordCodec, TYPE_TEXT, TYPE_INTEGER


class ColumnDef:
    """Represents a column definition in a table."""

    def __init__(self, name: str, col_type: str, is_primary_key: bool = False):
        self.name = name
        self.type = col_type  # 'INTEGER' or 'TEXT'
        self.is_primary_key = is_primary_key

    def __repr__(self) -> str:
        pk = " PRIMARY KEY" if self.is_primary_key else ""
        return f"ColumnDef({self.name}, {self.type}{pk})"


class TableDef:
    """Represents a table definition."""

    def __init__(self, name: str, columns: List[ColumnDef], root_page_id: int):
        self.name = name
        self.columns = columns
        self.root_page_id = root_page_id

        # Find the rowid column (INTEGER PRIMARY KEY)
        self.rowid_column: Optional[str] = None
        for col in columns:
            if col.is_primary_key and col.type == "INTEGER":
                self.rowid_column = col.name
                break

    def get_column_index(self, name: str) -> int:
        """Get the index of a column by name."""
        for i, col in enumerate(self.columns):
            if col.name == name:
                return i
        raise SchemaError(f"Column '{name}' not found in table '{self.name}'")

    def __repr__(self) -> str:
        return f"TableDef({self.name}, root_page={self.root_page_id})"


class Schema:
    """
    Manages database schema (tables, columns).

    Schema is persisted in the database file and loaded on open.
    For MVP, we use a simple in-memory cache with persistence via Pager.
    """

    def __init__(self, pager: Pager):
        self.pager = pager
        self.tables: Dict[str, TableDef] = {}
        self._load_schema()

    def _load_schema(self) -> None:
        """Load schema from the database file."""
        # For MVP, we store schema metadata in the header page (page 0)
        # Schema format: JSON string stored after the fixed header (offset 32+)
        try:
            page0 = self.pager.read_page(0)
            # Check if we have schema data (after 32-byte header)
            schema_data = page0[32:].rstrip(b'\x00').decode('utf-8', errors='ignore').strip()
            if schema_data:
                import json
                schema_dict = json.loads(schema_data)
                for table_name, table_info in schema_dict.items():
                    columns = []
                    for col_info in table_info['columns']:
                        col = ColumnDef(
                            name=col_info['name'],
                            col_type=col_info['type'],
                            is_primary_key=col_info['is_primary_key']
                        )
                        columns.append(col)
                    table_def = TableDef(
                        name=table_name,
                        columns=columns,
                        root_page_id=table_info['root_page_id']
                    )
                    self.tables[table_name] = table_def
        except (json.JSONDecodeError, KeyError, IndexError):
            # No schema or corrupted schema - start fresh
            pass

    def _save_schema(self) -> None:
        """Save schema to the database file."""
        # Serialize schema to JSON and write to page 0 (offset 32+)
        import json

        schema_dict = {}
        for table_name, table_def in self.tables.items():
            schema_dict[table_name] = {
                'root_page_id': table_def.root_page_id,
                'columns': [
                    {
                        'name': col.name,
                        'type': col.type,
                        'is_primary_key': col.is_primary_key
                    }
                    for col in table_def.columns
                ]
            }

        schema_json = json.dumps(schema_dict, ensure_ascii=False)
        schema_bytes = schema_json.encode('utf-8')

        # Read page 0
        page0 = self.pager.read_page(0)

        # Ensure page0 is writable (copy if needed)
        page0 = bytearray(page0)

        # Write schema JSON starting at offset 32, null-pad to fill page
        max_schema_size = len(page0) - 32
        if len(schema_bytes) > max_schema_size:
            raise CorruptDatabaseError("Schema too large to persist")

        # Clear the schema area first (offset 32 to end)
        for i in range(32, len(page0)):
            page0[i] = 0

        # Write schema bytes
        for i, byte in enumerate(schema_bytes):
            page0[32 + i] = byte

        # Write back the modified page 0
        self.pager.write_page(0, bytes(page0))

    def create_table(self, name: str, columns: List[ColumnDef]) -> int:
        """
        Create a new table.

        Returns the root page ID for the table's B+Tree.
        """
        if name in self.tables:
            raise SchemaError(f"Table '{name}' already exists")

        # Allocate a new page for the B+Tree root
        root_page_id = self.pager.allocate_page()

        table_def = TableDef(name, columns, root_page_id)
        self.tables[name] = table_def

        # Persist schema
        self._save_schema()

        return root_page_id

    def get_table(self, name: str) -> TableDef:
        """Get a table definition by name."""
        if name not in self.tables:
            raise SchemaError(f"Table '{name}' not found")
        return self.tables[name]

    def table_exists(self, name: str) -> bool:
        """Check if a table exists."""
        return name in self.tables

    def list_tables(self) -> List[str]:
        """List all table names."""
        return list(self.tables.keys())

    def get_rowid_column(self, table_name: str) -> Optional[str]:
        """Get the rowid column name for a table."""
        table = self.get_table(table_name)
        return table.rowid_column

    def validate_row(self, table_name: str, values: List) -> None:
        """
        Validate that values match the table's column types.

        Raises TypeMismatchError if types don't match.
        """
        from minisqlite.errors import TypeMismatchError

        table = self.get_table(table_name)

        if len(values) != len(table.columns):
            raise TypeMismatchError(
                f"Expected {len(table.columns)} values, got {len(values)}"
            )

        for i, (col, value) in enumerate(zip(table.columns, values)):
            if value is None:
                # NULL values are allowed for now (MVP doesn't enforce NOT NULL)
                continue

            if col.type == "INTEGER":
                if not isinstance(value, int):
                    raise TypeMismatchError(
                        f"Column '{col.name}' expects INTEGER, got {type(value).__name__}"
                    )
            elif col.type == "TEXT":
                if not isinstance(value, str):
                    raise TypeMismatchError(
                        f"Column '{col.name}' expects TEXT, got {type(value).__name__}"
                    )

    def get_sql_for_table(self, name: str) -> str:
        """Get the CREATE TABLE SQL for a table."""
        table = self.get_table(name)
        col_defs = []
        for col in table.columns:
            col_str = f"{col.name} {col.type}"
            if col.is_primary_key:
                col_str += " PRIMARY KEY"
            col_defs.append(col_str)

        return f"CREATE TABLE {name} ({', '.join(col_defs)})"