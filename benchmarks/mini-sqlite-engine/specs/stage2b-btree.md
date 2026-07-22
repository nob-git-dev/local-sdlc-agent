# Mini SQLite Engine Stage 2B SPEC: B+Tree

Build rowid-keyed B+Tree storage on top of the existing Pager.

## Existing Context

The project already has:

- `minisqlite/errors.py`
- `minisqlite/storage/file_format.py`
- `minisqlite/storage/pager.py`

Pager API summary:

```python
from minisqlite.storage.pager import Pager

pager = Pager(path)
page_id = pager.allocate_page()
page = pager.read_page(page_id)      # exactly PAGE_SIZE bytes
pager.write_page(page_id, data)      # data <= PAGE_SIZE, zero padded
pager.close()
```

`PAGE_SIZE = 4096`.

## Fixed Requirements

- Python standard library only.
- Do not use `sqlite3`, dbm, shelve, JSON DB storage, or an existing KVS.
- B+Tree must persist data through Pager pages.
- Do not implement SQL execution in this stage.

## Files To Create

- `minisqlite/storage/btree.py`
- `tests/test_btree.py`

## B+Tree Scope

Keys:

- signed integer rowid

Values:

- opaque `bytes` payload

Required operations:

- `search(rowid) -> bytes | None`
- `insert(rowid, payload) -> None`
- `delete(rowid) -> bool`
- `scan_all() -> list[tuple[int, bytes]]` in rowid ascending order

Required behavior:

- Empty tree search returns `None`.
- One inserted key can be searched.
- Multiple keys scan in rowid order.
- Duplicate rowid raises `DuplicateKeyError`.
- Inserting enough rows must split leaf pages.
- Inserting enough rows must create an internal root page.
- Delete removes the key.
- Deleted key is not found after close/reopen.
- Large insertion after close/reopen can search all keys.

## Page Format Guidance

Use these page types:

- LEAF = 1
- INTERNAL = 2

You may implement a simple B+Tree as long as pages are persisted and split occurs. A practical MVP is:

- Leaf pages store sorted `(rowid, payload)` cells.
- Internal root stores child page ids and max keys.
- Leaf pages have right sibling links to support ordered scan.
- Parent merge/rebalance after delete is not required.

The implementation may rebuild a small internal root after leaf split/delete if that is simpler. Document assumptions in code comments or tests.

## Public API

Expose an easy-to-use class, for example:

```python
tree = BTree(pager, root_page_id=None)
root_page_id = tree.root_page_id
tree.insert(1, b"payload")
tree.search(1)
tree.scan_all()
tree.delete(1)
```

If `root_page_id` is `None`, allocate a new root leaf page.
If `root_page_id` is supplied, load the existing tree from disk.

## Required Tests

- Empty B+Tree search returns `None`.
- One insert/search works.
- Multiple inserts scan in rowid order.
- Duplicate rowid raises `DuplicateKeyError`.
- Delete works.
- Delete persists after close/reopen.
- Enough inserts cause more than one page allocation.
- Enough inserts can still search every row after close/reopen.
- Scan order remains rowid ascending after split.

## Acceptance Checks

- `python3 -m compileall -q minisqlite tests`
- `python3 -m unittest tests.test_btree`

