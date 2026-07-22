# MiniSQLite Engine

A minimal SQLite-like database engine implemented in Python.

## Features

- SQL parsing and execution
- CREATE TABLE, INSERT, SELECT, DELETE statements
- INTEGER and TEXT data types
- rowid management with automatic increment
- B+Tree storage engine
- Single-file database persistence
- CLI and Python API interfaces

## Installation

No installation required. The engine is implemented in pure Python.

## Usage

### CLI Mode

#### Interactive Mode

```bash
python -m minisqlite sample.db
```

Example session:

```text
minisqlite> CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);
OK
minisqlite> INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);
OK
minisqlite> INSERT INTO users (name, age) VALUES ('Bob', 25);
OK
minisqlite> SELECT * FROM users;
id|name|age
1|Alice|30
2|Bob|25
minisqlite> SELECT name FROM users WHERE age >= 30;
name
Alice
minisqlite> DELETE FROM users WHERE id = 2;
OK
minisqlite> SELECT * FROM users;
id|name|age
1|Alice|30
minisqlite> .tables
users
minisqlite> .schema users
CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)
minisqlite> .exit
```

#### Single SQL Execution

```bash
python -m minisqlite sample.db "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"
python -m minisqlite sample.db "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);"
python -m minisqlite sample.db "SELECT * FROM users;"
```

### Python API

```python
from minisqlite import connect

# Connect to a database (creates file if it doesn't exist)
conn = connect("sample.db")

# Execute SQL statements
conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);")
conn.execute("INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);")
conn.execute("INSERT INTO users (name, age) VALUES ('Bob', 25);")

# Execute SELECT and get results
result = conn.execute("SELECT * FROM users;")
print(result.columns)  # ['id', 'name', 'age']
print(result.rows)     # [[1, 'Alice', 30], [2, 'Bob', 25]]

# Execute DELETE
result = conn.execute("DELETE FROM users WHERE id = 2;")
print(result.rowcount)  # 1

# Close connection
conn.close()
```

## Supported SQL

### Data Definition Language (DDL)

- `CREATE TABLE table_name (column_name column_type [PRIMARY KEY], ...)`

### Data Manipulation Language (DML)

- `INSERT INTO table_name [(column1, column2, ...)] VALUES (value1, value2, ...)`
- `SELECT column_list FROM table_name [WHERE condition]`
- `DELETE FROM table_name WHERE condition`

### Data Types

- `INTEGER`: 64-bit signed integer
- `TEXT`: UTF-8 encoded string

### WHERE Clause Conditions

- `column = value`
- `column != value`
- `column > value`
- `column >= value`
- `column < value`
- `column <= value`

### Special Commands (CLI only)

- `.tables`: List all tables
- `.schema table_name`: Show CREATE TABLE statement
- `.exit` or `.quit`: Exit the CLI
- `.help`: Show help message
- `.mode`: Show current mode (placeholder)
- `.headers`: Toggle headers (placeholder)

## Architecture

### Components

- **Lexer**: Tokenizes SQL input
- **Parser**: Converts tokens to Abstract Syntax Tree (AST)
- **Executor**: Executes AST against the storage engine
- **Schema Manager**: Manages table definitions
- **Pager**: Handles page-level file I/O
- **B+Tree**: Implements the storage index structure
- **Record Codec**: Encodes/decodes row data

### File Format

- Fixed 4096-byte pages
- Magic bytes: `MSQLITE1`
- Header page (page 0) contains metadata
- B+Tree pages store table data
- Schema stored in internal B+Tree

## Design Decisions

### Case Sensitivity

- SQL keywords are case-insensitive
- Table names and column names are case-sensitive (stored as-is)

### Comparison Rules

- INTEGER values are compared numerically
- TEXT values are compared lexicographically
- Type mismatches in comparisons result in no match (not an error)

### DELETE Restrictions

- WHERE clause is required for DELETE statements
- Full table deletion without WHERE is not allowed for safety

### B+Tree Implementation

- Leaf pages store actual row data
- Internal pages store routing information
- Page splitting on overflow
- No page merging or reuse (simplified MVP)

## Limitations

The following features are NOT implemented in this MVP:

- JOIN operations
- GROUP BY / HAVING
- ORDER BY (results are returned in rowid order)
- Subqueries
- Transactions / WAL
- Indexes (other than implicit rowid)
- ALTER TABLE
- UPDATE statements
- Foreign keys
- CHECK constraints
- DEFAULT values
- NULL values (except as optional extension)
- Multiple concurrent connections

## Testing

Run all tests:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

## License

This is a demonstration project for educational purposes.