# Mini SQLite Engine Stage 2A SPEC: File Format and Pager

Build only file format constants, fixed-size page storage, header read/write, and pager tests.

## Fixed Requirements

- Python standard library only.
- Do not use `sqlite3`, dbm, shelve, or JSON-file database storage.
- Store data in a single binary file with fixed-size pages.
- Keep pager independent from SQL parser, executor, and B+Tree logic.

## Files To Create

- `minisqlite/storage/file_format.py`
- `minisqlite/storage/pager.py`
- `tests/test_pager.py`

## Existing Context

The project already has `minisqlite/errors.py` with `CorruptDatabaseError`.

## File Format

Page size is fixed:

```python
PAGE_SIZE = 4096
```

Page 0 is the database header page.

Header layout, all integers big-endian:

| Offset | Size | Field |
|---:|---:|---|
| 0 | 8 | magic bytes `MSQLITE1` |
| 8 | 4 | page_size, must be 4096 |
| 12 | 4 | format_version, must be 1 |
| 16 | 4 | next_page_id |
| 20 | 4 | schema_root_page |
| 24 | 8 | reserved |
| 32 | rest | reserved/schema bytes for later |

`page_id` starts at 0. Page 0 is reserved. Page 1+ are available for B+Tree pages.

## Pager API

Provide a small API that later B+Tree/schema code can import. Exact class names are flexible, but tests must cover:

- opening a new DB file initializes header page
- opening an existing DB validates magic/page size/version
- allocate_page returns the next page id and increments header `next_page_id`
- read_page(page_id) returns exactly 4096 bytes
- write_page(page_id, data) writes exactly one page; shorter data can be padded with zero bytes, longer data must error
- close flushes header/page changes

Raise `CorruptDatabaseError` for:

- bad magic bytes
- wrong page size
- unsupported format version
- reading a page beyond EOF

## Required Tests

- New DB file is created with valid header.
- Header can be read after close/reopen.
- Page can be written and read back.
- allocate_page increments page ids.
- Bad magic bytes are detected.
- Oversized page write errors.
- Reading missing page errors.

## Acceptance Checks

- `python3 -m compileall -q minisqlite tests`
- `python3 -m unittest tests.test_pager`

