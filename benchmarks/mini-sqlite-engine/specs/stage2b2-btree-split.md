# Mini SQLite Engine Stage 2B2 SPEC: Multi-Leaf B+Tree Split

Extend the existing persistent single-leaf `BTree` into a small multi-leaf
B+Tree that can split leaf pages and persist an internal root.

## Existing Context

Already implemented:

- `minisqlite/storage/pager.py`
- `minisqlite/storage/file_format.py`
- `minisqlite/storage/btree.py`
- `tests/test_btree.py`

Current `BTree` public API must remain compatible:

```python
tree = BTree(pager, root_page_id=None)
tree.root_page_id
tree.search(rowid) -> bytes | None
tree.insert(rowid, payload) -> None
tree.delete(rowid) -> bool
tree.scan_all() -> list[tuple[int, bytes]]
```

## Fixed Requirements

- Python standard library only.
- Do not use `sqlite3`, dbm, shelve, JSON database storage, or an existing KVS.
- Values are opaque `bytes` payloads.
- Data must persist through Pager pages.
- Do not implement SQL execution in this stage.
- Keep existing single-leaf tests passing.

## Scope

Implement enough B+Tree behavior for tables larger than one page:

- A root page may be either a leaf root or an internal root.
- Leaf pages store sorted `(rowid, payload)` cells.
- When inserting would overflow a leaf, split into two or more leaf pages.
- The tree must allocate additional pages through `Pager.allocate_page()`.
- An internal root must be persisted and must route searches to leaf pages.
- `scan_all()` must return all rows in ascending rowid order across leaves.
- `delete(rowid)` must remove the key and persist the deletion.

Rebalancing and merge after delete are not required. Empty leaves after delete
are acceptable if search/scan semantics remain correct.

## Implementation Guidance

Efficiency is not the goal. A simple, reliable MVP is acceptable:

1. Load all current rows with `scan_all()`.
2. Apply insert/delete in memory with duplicate detection.
3. Repack rows into one or more leaf pages that fit within `PAGE_SIZE`.
4. If one leaf is enough, store a leaf root.
5. If multiple leaves are needed, allocate/write leaf pages and write an
   internal root containing `(max_key, child_page_id)` entries.

The exact binary layout may differ from Stage 2B1, but it must be robustly
parsed and backward-compatible enough for existing Stage 2B1 tests created in
the same run.

Recommended page concepts:

- Leaf page magic/type.
- Internal root magic/type.
- Leaf cell count and cell byte area.
- Internal root child count and repeated `(max_key, child_page_id)` entries.

If you choose to rebuild the root when splitting, `tree.root_page_id` may remain
the original page id by rewriting that page as the internal root. This avoids
requiring callers to discover a new root id after the first split.

## Required Tests

Add tests without weakening existing tests:

- Inserting enough rows allocates more than one page.
- Inserting enough rows creates a non-leaf/internal root or equivalent persisted
  root routing metadata.
- After close/reopen with `root_page_id`, every inserted row can be searched.
- `scan_all()` remains rowid ascending after split, including out-of-order
  inserts.
- Delete after split removes the key, persists after close/reopen, and does not
  remove neighboring keys.

Use payloads large enough to force a split with a modest row count, for example
50 to 200 rows with 80 to 200 byte payloads.

## Acceptance Checks

- `python3 -m compileall -q minisqlite tests`
- `python3 -m unittest tests.test_btree`
