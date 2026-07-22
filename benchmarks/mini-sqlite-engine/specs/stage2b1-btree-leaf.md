# Mini SQLite Engine Stage 2B1 SPEC: Single-Leaf BTree

Build the first BTree implementation as a persistent single leaf page.
Split/internal pages are explicitly not required in this stage.

## Existing Context

Use existing:

- `minisqlite/errors.py`
- `minisqlite/storage/file_format.py`
- `minisqlite/storage/pager.py`

Pager API:

```python
pager = Pager(path)
page_id = pager.allocate_page()
page = pager.read_page(page_id)
pager.write_page(page_id, data)
pager.close()
```

## Fixed Requirements

- Python standard library only.
- Do not use sqlite3/dbm/shelve/JSON database storage.
- Values are opaque bytes.
- Persist through Pager pages.

## Files To Create

- `minisqlite/storage/btree.py`
- `tests/test_btree.py`

## Public API

Provide:

```python
tree = BTree(pager, root_page_id=None)
tree.root_page_id
tree.search(rowid) -> bytes | None
tree.insert(rowid, payload) -> None
tree.delete(rowid) -> bool
tree.scan_all() -> list[tuple[int, bytes]]
```

If `root_page_id is None`, allocate and initialize one leaf root page.
If `root_page_id` is provided, load that leaf root page.

## Single Leaf Format

You may choose the exact binary layout, but it must fit in one 4096-byte page and be robustly parsed.

Minimum behavior:

- Store sorted `(rowid, payload)` cells.
- Duplicate rowid raises `DuplicateKeyError`.
- Page overflow raises `StorageError` or `ValueError`.
- Corrupt page type raises `CorruptDatabaseError`.

## Required Tests

- Empty tree search returns `None`.
- One insert/search works.
- Multiple inserts scan in rowid ascending order even if inserted out of order.
- Duplicate rowid raises `DuplicateKeyError`.
- Delete returns True for existing row and False for missing row.
- Deleted key is not found.
- Data persists after close/reopen using `root_page_id`.
- Page overflow is detected by inserting large payloads or many rows.

## Acceptance Checks

- `python3 -m compileall -q minisqlite tests`
- `python3 -m unittest tests.test_btree`

