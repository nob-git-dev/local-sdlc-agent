# Mini SQLite Engine Stage 3A SPEC: connect() and CREATE TABLE

Implement the smallest end-to-end connection layer:

- `from minisqlite import connect`
- `conn = connect(path)`
- `conn.execute("CREATE TABLE ...;")`
- schema metadata persists across `close()` and reopen

Do not implement INSERT/SELECT/DELETE in this stage except for clear stubs that
raise `NotImplementedError` or `SchemaError`.

## Existing Context

Use existing:

- `minisqlite/sql/parser.py`
- `minisqlite/sql/ast.py`
- `minisqlite/storage/pager.py`
- `minisqlite/storage/btree.py`
- `minisqlite/errors.py`

## Fixed Requirements

- Python standard library only.
- Do not use `sqlite3`, dbm, shelve, or an existing database/KVS.
- Keep all existing tests passing.
- New table roots must be initialized with `BTree(pager)` and then persisted in
  the schema catalog. Do not pass an uninitialized allocated page id into
  `BTree(pager, root_page_id=...)`.

## Schema Catalog

Persist schema metadata in page 1 for connection-layer databases.

MVP format may be JSON metadata stored in a fixed page:

```json
{
  "tables": {
    "users": {
      "columns": [
        {"name": "id", "type": "INTEGER", "primary_key": true},
        {"name": "name", "type": "TEXT", "primary_key": false}
      ],
      "root_page_id": 2,
      "next_rowid": 1
    }
  }
}
```

JSON is allowed for schema metadata only. Row data must be stored later in
BTree pages, not JSON.

## Public API

Create/update:

- `minisqlite/__init__.py`
- `minisqlite/connection.py`
- `tests/test_connection.py`

Required shape:

```python
from minisqlite import connect

conn = connect(path)
result = conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);")
conn.close()
```

`Result` dataclass:

```python
@dataclass
class Result:
    columns: list[str]
    rows: list[list[object]]
    rows_affected: int = 0
```

## Required Tests

- `connect` is importable from `minisqlite`.
- `CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);`
  returns empty `Result`.
- Creating the same table twice raises `SchemaError`.
- Reopening the database preserves the table metadata. This can be tested with
  a public helper/property if needed, or by asserting a second `CREATE TABLE
  users ...` after reopen raises `SchemaError`.
- Table root page is initialized: the persisted `root_page_id` can be loaded by
  `BTree(pager, root_page_id=...)` without `CorruptDatabaseError`.

## Acceptance Checks

- `python3 -m compileall -q minisqlite tests`
- `python3 -m unittest tests.test_connection`
- `python3 -m unittest discover -s tests`
