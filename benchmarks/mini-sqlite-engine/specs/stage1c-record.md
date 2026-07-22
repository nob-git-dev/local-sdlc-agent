# Mini SQLite Engine Stage 1C SPEC: Record Codec

Build only the binary record codec and tests.

## Existing Context

The project already has `minisqlite/errors.py`.

## Fixed Requirements

- Python standard library only.
- Do not use `sqlite3`.
- Do not use JSON serialization for records.
- Keep record codec independent from pager, B+Tree, SQL parser, and file I/O.

## Files To Create

- `minisqlite/storage/record.py`
- `tests/test_record.py`

## Error Type

Malformed or truncated payloads must raise `CorruptDatabaseError` from `minisqlite.errors`.

## Record Format

```text
column_count: 2 bytes unsigned big-endian
value_1
value_2
...
value_n
```

Value encodings:

- NULL: type byte `0`
- INTEGER: type byte `1`, then 8-byte signed big-endian integer
- TEXT: type byte `2`, then 4-byte unsigned big-endian byte length, then UTF-8 bytes

## Public API

Provide simple functions that later storage/executor code can import, for example:

- `encode_record(values: list[object]) -> bytes`
- `decode_record(data: bytes) -> list[object]`

Using tuples or sequences internally is fine, but tests should exercise the public API.

## Required Tests

- INTEGER round trip.
- TEXT round trip.
- Multiple columns round trip.
- UTF-8 Japanese round trip.
- `None` round trip if NULL support is implemented.
- Truncated column count raises `CorruptDatabaseError`.
- Truncated integer raises `CorruptDatabaseError`.
- Truncated text length or payload raises `CorruptDatabaseError`.
- Unknown type tag raises `CorruptDatabaseError`.

## Acceptance Checks

- `python3 -m compileall -q minisqlite tests`
- `python3 -m unittest tests.test_record`

