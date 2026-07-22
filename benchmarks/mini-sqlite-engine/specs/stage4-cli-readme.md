# Mini SQLite Engine Stage 4 SPEC: CLI and README

Add the user-facing CLI and README for the implemented Mini SQLite Engine.

## Existing Context

Already implemented:

- `from minisqlite import connect`
- `Connection.execute()` supports CREATE, INSERT, SELECT, DELETE
- schema/data persist across reopen

## Fixed Requirements

- Python standard library only.
- Keep all existing tests passing.
- Do not use `sqlite3`, dbm, shelve, or an existing database/KVS.

## CLI Requirements

Support:

```bash
python -m minisqlite sample.db "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
python -m minisqlite sample.db "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);"
python -m minisqlite sample.db "SELECT * FROM users;"
python -m minisqlite sample.db
```

Implementation target:

- Add `minisqlite/__main__.py`.

Single SQL mode:

- Execute the SQL and exit.
- For CREATE/INSERT/DELETE, print `OK` unless a row count is more appropriate.
- For SELECT, print a pipe-delimited table:

```text
id|name|age
1|Alice|30
```

Interactive mode:

- Prompt: `minisqlite> `
- `.exit` exits.
- `.tables` prints table names, one per line.
- `.schema users` prints a simple CREATE TABLE statement for that table.
- SQL input is one line per statement for this stage.

If exposing tables/schema requires a small public/private helper on
`Connection`, add it with tests.

## README Requirements

Add `README.md` under the mini-sqlite project root documenting:

- What is implemented.
- What is intentionally not implemented.
- Python API example.
- CLI examples.
- Test command.
- Note that schema metadata uses JSON in page 1 but row data is stored in BTree
  pages as encoded records.

## Required Tests

Add tests:

- `python -m minisqlite db "CREATE TABLE ..."` exits 0 and prints OK.
- A sequence of CLI invocations can create/insert/select persisted data.
- SELECT CLI output contains header and row.
- `.tables` and `.schema users` can be tested through helper functions or a
  subprocess interactive input if simple.

## Acceptance Checks

- `python3 -m compileall -q minisqlite tests`
- `python3 -m unittest discover -s tests`
