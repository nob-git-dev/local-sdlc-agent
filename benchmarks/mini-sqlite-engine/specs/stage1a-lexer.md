# Mini SQLite Engine Stage 1A SPEC: Lexer

Build only the common errors and SQL lexer for Mini SQLite Engine.

## Fixed Requirements

- Python standard library only.
- Do not use `sqlite3`.
- Do not use an existing SQL parser.
- The lexer must be independent from parser, storage, and file I/O.

## Files To Create

- `minisqlite/__init__.py`
- `minisqlite/errors.py`
- `minisqlite/sql/__init__.py`
- `minisqlite/sql/lexer.py`
- `tests/test_lexer.py`

## Error Types

Define at least:

- `MiniSQLiteError`
- `SQLSyntaxError`
- `SchemaError`
- `TypeMismatchError`
- `DuplicateKeyError`
- `StorageError`
- `CorruptDatabaseError`

Lexer failures must raise `SQLSyntaxError`.

## Lexer Scope

Tokenize SQL for later parser stages.

Supported token kinds:

- keyword
- identifier
- integer
- string
- symbol
- operator
- eof

Keywords are case-insensitive and include:

- `CREATE`, `TABLE`, `INTEGER`, `TEXT`, `PRIMARY`, `KEY`
- `INSERT`, `INTO`, `VALUES`
- `SELECT`, `FROM`, `WHERE`
- `DELETE`

Identifiers:

- Pattern: `[A-Za-z_][A-Za-z0-9_]*`

Integer literals:

- `123`, `-10`, `0`

String literals:

- Single quoted.
- `''` inside a string decodes to one single quote.
- Example: `'It''s OK'` becomes `It's OK`.

Symbols/operators:

- `(`, `)`, `,`, `;`, `*`
- `=`, `!=`, `>`, `>=`, `<`, `<=`

Whitespace is ignored.

## Required Tests

- Keywords are recognized case-insensitively.
- Identifiers are recognized.
- Integer literals are recognized.
- Text literals are recognized.
- `It''s OK` decodes to `It's OK`.
- Operators are recognized.
- Unterminated strings raise `SQLSyntaxError`.
- Unexpected characters raise `SQLSyntaxError`.

## Acceptance Checks

- `python3 -m compileall -q minisqlite tests`
- `python3 -m unittest tests.test_lexer`

