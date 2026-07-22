# Mini SQLite Engine Stage 1B SPEC: Parser and AST

Build only SQL AST dataclasses, parser, and parser tests.

## Existing Context

The project already has:

- `minisqlite/errors.py`
- `minisqlite/sql/lexer.py`
- `tests/test_lexer.py`

The parser must use the existing lexer API.

Existing lexer API:

```python
from minisqlite.sql.lexer import tokenize, Token, TokenType

class TokenType(Enum):
    KEYWORD
    IDENTIFIER
    INTEGER
    STRING
    SYMBOL
    OPERATOR
    EOF

@dataclass(frozen=True)
class Token:
    type: TokenType
    value: str
```

`tokenize(sql: str) -> list[Token]` returns a trailing EOF token.
Keyword token values preserve original spelling, so parser keyword checks should compare `token.value.upper()`.

## Fixed Requirements

- Python standard library only.
- Do not use `sqlite3`.
- Do not use an existing SQL parser.
- Keep parser independent from storage and file I/O.
- Do not modify lexer/errors unless the tests clearly prove the existing API is insufficient.

## Files To Create

- `minisqlite/sql/ast.py`
- `minisqlite/sql/parser.py`
- `tests/test_parser.py`

## Parser Scope

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

Literals:

- INTEGER
- TEXT

WHERE condition:

- One condition only.
- Operators: `=`, `!=`, `>`, `>=`, `<`, `<=`

DELETE without WHERE must raise `SQLSyntaxError`.

## AST Expectations

Use dataclasses for:

- `ColumnDef(name: str, type_name: str, primary_key: bool = False)`
- `Literal(value: object, type_name: str)`
- `Condition(column: str, operator: str, value: Literal)`
- `CreateTable(table_name: str, columns: list[ColumnDef])`
- `Insert(table_name: str, columns: list[str] | None, values: list[Literal])`
- `Select(table_name: str, columns: list[str] | None, where: Condition | None)`
  - Use `columns is None` to mean `SELECT *`.
- `Delete(table_name: str, where: Condition)`

The public parser entry point should be easy for later executor code to import, for example `parse(sql: str)`.

## Required Tests

- CREATE TABLE parses table name, columns, types, and INTEGER PRIMARY KEY.
- INSERT with column list parses.
- INSERT without column list parses.
- SELECT `*` parses with `columns is None`.
- SELECT column list parses.
- WHERE condition parses with operator and literal.
- DELETE with WHERE parses.
- DELETE without WHERE raises `SQLSyntaxError`.
- Invalid syntax raises `SQLSyntaxError`.
- Existing lexer tests still pass.

## Acceptance Checks

- `python3 -m compileall -q minisqlite tests`
- `python3 -m unittest tests.test_lexer tests.test_parser`
