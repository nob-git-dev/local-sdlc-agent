# Mini SQLite Engine Stage 3B SPEC: INSERT, SELECT, DELETE

Extend the existing Stage 3A connection layer with row operations.

## Existing Context

Already implemented and tested:

- `from minisqlite import connect`
- `Connection.execute("CREATE TABLE ...;")`
- schema catalog in page 1
- table `root_page_id` initialized by `BTree(pager)`

Use existing:

- `minisqlite/sql/parser.py`
- `minisqlite/sql/ast.py`
- `minisqlite/storage/record.py`
- `minisqlite/storage/btree.py`
- `minisqlite/errors.py`

## Fixed Requirements

- Python standard library only.
- Do not use `sqlite3`, dbm, shelve, JSON row storage, or an existing KVS.
- Row data must be stored in table BTree pages as `encode_record` payloads.
- Keep all existing tests passing.
- Do not weaken Stage 3A tests.

## Required Behavior

Implement in `Connection.execute(sql)`:

- `INSERT INTO table (...) VALUES (...);`
- `SELECT * FROM table;`
- `SELECT col1, col2 FROM table;`
- `SELECT col FROM table WHERE id = 1;`
- `DELETE FROM table WHERE id = 1;`

All tables have an internal rowid:

- `INTEGER PRIMARY KEY` is rowid alias.
- If no primary key exists, auto-generate `next_rowid` and persist it in schema.
- Duplicate rowid raises `DuplicateKeyError`.
- Unknown table/column raises `SchemaError`.
- Type mismatch raises `TypeMismatchError`.

Type checks:

- `INTEGER` accepts Python `int`.
- `TEXT` accepts Python `str`.
- Missing non-primary-key columns may be stored as `None`.

SELECT:

- Return a `Result(columns=[...], rows=[[...]])`.
- `SELECT *` returns declared columns in schema order.
- Rows may be returned in rowid order.
- WHERE equality on any declared column should work by scanning rows.

DELETE:

- Equality on rowid / primary key must delete through `BTree.delete(rowid)`.
- Return `rows_affected`.
- Persist deletion after close/reopen.

## Files To Update

- `minisqlite/connection.py`
- `tests/test_connection.py`

Update `minisqlite/__init__.py` only if needed.

## Required Tests

Add tests:

- Insert one row and `SELECT *` returns `["id", "name", "age"]` and
  `[[1, "Alice", 30]]`.
- Insert rows out of order and `SELECT *` returns all rows.
- `SELECT name FROM users WHERE id = 1;` returns `[["Alice"]]`.
- Data persists after close and reopen.
- Duplicate primary key raises `DuplicateKeyError`.
- Type mismatch raises `TypeMismatchError`.
- `DELETE FROM users WHERE id = 1;` removes row and persists after reopen.

## Acceptance Checks

- `python3 -m compileall -q minisqlite tests`
- `python3 -m unittest tests.test_connection`
- `python3 -m unittest discover -s tests`
