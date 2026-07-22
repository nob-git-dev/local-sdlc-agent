# Mini SQLite Engine Stage 3 SPEC: Connection API and SQL Executor

Implement the first end-to-end database layer on top of the existing parser,
record codec, pager, and B+Tree.

## Existing Context

Already implemented:

- `minisqlite/sql/parser.py`
- `minisqlite/sql/ast.py`
- `minisqlite/storage/record.py`
- `minisqlite/storage/pager.py`
- `minisqlite/storage/btree.py`
- `minisqlite/errors.py`

## Fixed Requirements

- Python standard library only.
- Do not use `sqlite3`, dbm, shelve, or an existing database/KVS.
- Row data must be stored in `BTree` pages as encoded records.
- SQL must be parsed through the existing parser.
- Keep all existing tests passing.

## Public API

Expose this API from `minisqlite.__init__`:

```python
from minisqlite import connect

conn = connect("sample.db")
result = conn.execute("SELECT * FROM users;")
conn.close()
```

`connect(path)` returns a `Connection`.

`Connection.execute(sql: str)` supports:

- `CREATE TABLE`
- `INSERT INTO`
- `SELECT`
- `DELETE`

Return values:

```python
@dataclass
class Result:
    columns: list[str]
    rows: list[list[object]]
    rows_affected: int = 0
```

For `CREATE`, `INSERT`, and `DELETE`, `columns` and `rows` may be empty and
`rows_affected` should reflect affected rows where applicable.

## Schema Catalog

Persist schema metadata in the database file. A simple MVP is acceptable:

- Reserve page 1 as a schema catalog page for databases created by the
  connection layer.
- Store table metadata including:
  - table name
  - columns in declaration order
  - column type (`INTEGER` or `TEXT`)
  - primary key column name if present
  - table BTree root page id
- The exact schema page binary format may be simple. JSON is acceptable for
  schema metadata only; row data must not be JSON-backed.
- Reopening a database must load the schema catalog and table root page ids.

## Table Semantics

All tables have an internal rowid.

- If a column is `INTEGER PRIMARY KEY`, that column is the rowid alias.
- If no primary key exists, auto-generate rowid as current max rowid + 1.
- Duplicate rowid raises `DuplicateKeyError`.
- Unknown table or column raises `SchemaError`.
- Type mismatch raises `TypeMismatchError`.

Record payload:

- Store values in declared column order using `encode_record`.
- Decode with `decode_record`.
- For an `INTEGER PRIMARY KEY` alias, the stored row should still include the
  primary key column value so `SELECT *` returns declared columns.

## SELECT Scope

Support:

```sql
SELECT * FROM table;
SELECT col1, col2 FROM table;
SELECT * FROM table WHERE id = 1;
SELECT * FROM table WHERE name = 'Alice';
```

Only equality predicates are required in this stage. Other operators may raise
`SQLSyntaxError` or `SchemaError`.

## DELETE Scope

Support:

```sql
DELETE FROM table WHERE id = 1;
```

Equality on the rowid / primary-key column is required. Equality on non-rowid
columns may be implemented by scan-delete, but is optional for this stage.

## Files To Create or Update

Likely files:

- `minisqlite/__init__.py`
- `minisqlite/connection.py`
- `tests/test_connection.py`

Update other files only when the added tests prove it is necessary.

## Required Tests

Add end-to-end tests:

- `connect()` is importable from `minisqlite`.
- `CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);`
- Insert one row and `SELECT *` returns `columns == ["id", "name", "age"]`
  and `rows == [[1, "Alice", 30]]`.
- Insert rows out of order and `SELECT *` returns all rows.
- `SELECT name FROM users WHERE id = 1;` returns only the requested column.
- Data persists after `close()` and reopen.
- Duplicate primary key raises `DuplicateKeyError`.
- Type mismatch raises `TypeMismatchError`.
- `DELETE FROM users WHERE id = 1;` removes the row and persists.

## Acceptance Checks

- `python3 -m compileall -q minisqlite tests`
- `python3 -m unittest discover -s tests`
