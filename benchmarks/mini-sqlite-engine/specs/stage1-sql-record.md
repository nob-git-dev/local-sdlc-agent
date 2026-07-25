# Mini SQLite Engine Stage 1 SPEC: SQL and Record Codec

Source: user-provided implementation specification captured for this benchmark.

## Purpose

Build the first layer of Mini SQLite Engine: SQL tokenization/parsing, common result/error types, and binary record encoding/decoding.

This stage does not implement pager, B+Tree, executor, CLI, or persistence. It must produce code that those later stages can import.

## Fixed Requirements

- Use Python standard library only.
- Do not use `sqlite3`.
- Do not use an existing SQL parser.
- Do not use SQLAlchemy, ORM libraries, dbm, shelve, or an existing KVS.
- Keep parser and record codec independent from file I/O.
- Provide tests.

## Files To Create

- `minisqlite/__init__.py`
- `minisqlite/result.py`
- `minisqlite/errors.py`
- `minisqlite/sql/__init__.py`
- `minisqlite/sql/ast.py`
- `minisqlite/sql/lexer.py`
- `minisqlite/sql/parser.py`
- `minisqlite/storage/__init__.py`
- `minisqlite/storage/record.py`
- `tests/test_lexer.py`
- `tests/test_parser.py`
- `tests/test_record.py`

## SQL Scope

Support these statements:

- `CREATE TABLE table_name (column_name column_type [PRIMARY KEY], ...);`
- `INSERT INTO table_name (column1, column2, ...) VALUES (value1, value2, ...);`
- `INSERT INTO table_name VALUES (value1, value2, ...);`
- `SELECT * FROM table_name [WHERE condition];`
- `SELECT column1, column2 FROM table_name [WHERE condition];`
- `DELETE FROM table_name WHERE condition;`

Supported types:

- `INTEGER`
- `TEXT`

Identifiers:

- Pattern: `[A-Za-z_][A-Za-z0-9_]*`
- SQL keywords are case-insensitive.

Literals:

- INTEGER: `123`, `-10`, `0`
- TEXT: single quoted, with `''` escaping. Example: `'It''s OK'`
- NULL may be parsed if convenient, but it is optional for later MVP.

WHERE condition:

- One condition only.
- Operators: `=`, `!=`, `>`, `>=`, `<`, `<=`

DELETE:

- `DELETE FROM table WHERE condition` only.
- `DELETE FROM table` without WHERE must be syntax error or later executor error. Prefer syntax error in this stage.

## AST Expectations

Use clear dataclasses for at least:

- `CreateTable`
- `Insert`
- `Select`
- `Delete`
- `ColumnDef`
- `Condition`
- `Literal`

The parser should return statement objects that later executor code can consume.

## Error Types

Define:

- `MiniSQLiteError`
- `SQLSyntaxError`
- `SchemaError`
- `TypeMismatchError`
- `DuplicateKeyError`
- `StorageError`
- `CorruptDatabaseError`

Lexer/parser syntax failures must raise `SQLSyntaxError`.
Record corruption must raise `CorruptDatabaseError`.

## Record Codec

Encode one row as:

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

Record codec must:

- Encode and decode INTEGER.
- Encode and decode TEXT.
- Encode and decode multiple columns.
- Preserve UTF-8 Japanese text.
- Detect truncated or malformed payloads and raise `CorruptDatabaseError`.

## Required Tests

Lexer tests:

- Keywords are recognized case-insensitively.
- Identifiers are recognized.
- Integer literals are recognized.
- Text literals are recognized.
- `It''s OK` decodes to `It's OK`.
- Unterminated strings raise `SQLSyntaxError`.

Parser tests:

- CREATE TABLE parses.
- INSERT with column list parses.
- INSERT without column list parses.
- SELECT `*` parses.
- SELECT column list parses.
- WHERE condition parses.
- DELETE with WHERE parses.
- Invalid syntax raises `SQLSyntaxError`.

Record tests:

- INTEGER round trip.
- TEXT round trip.
- Multiple columns round trip.
- UTF-8 Japanese round trip.
- Corrupt/truncated payload raises `CorruptDatabaseError`.

## Acceptance Checks

- `python3 -m compileall -q minisqlite tests`
- `python3 -m unittest tests.test_lexer tests.test_parser tests.test_record`
