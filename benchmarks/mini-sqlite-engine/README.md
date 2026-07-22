# Mini SQLite Engine

A minimal SQLite-compatible database engine implemented in Python from scratch.

## What is Implemented

- **SQL Support**: CREATE TABLE, INSERT, SELECT, DELETE
- **Data Types**: INTEGER, TEXT
- **Primary Keys**: INTEGER PRIMARY KEY with auto-increment
- **WHERE Clauses**: Basic comparison operators (=, !=, >, >=, <, <=)
- **Persistence**: Data persists across database file reopen
- **CLI Interface**: Command-line interface for single SQL execution and interactive mode
- **Schema Management**: Table metadata stored in JSON on page 1

## What is Intentionally Not Implemented

- JOINs
- Subqueries
- Aggregate functions (COUNT, SUM, AVG, etc.)
- UPDATE statements
- Transactions
- Indexes (other than primary key)
- Foreign keys
- Complex WHERE conditions (AND, OR, IN, LIKE, etc.)
- Multiple databases
- Concurrent access

## Python API Example

```python
from minisqlite import connect

# Connect to a database
conn = connect("my_database.db")

# Create a table
conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);")

# Insert data
conn.execute("INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);")
conn.execute("INSERT INTO users (id, name, age) VALUES (2, 'Bob', 40);")

# Query data
result = conn.execute("SELECT * FROM users;")
print(result.columns)  # ['id', 'name', 'age']
print(result.rows)     # [[1, 'Alice', 30], [2, 'Bob', 40]]

# Close connection
conn.close()
```

## CLI Examples

### Single SQL Execution

```bash
# Create a table
python -m minisqlite sample.db "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);"

# Insert data
python -m minisqlite sample.db "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);"

# Query data
python -m minisqlite sample.db "SELECT * FROM users;"
# Output:
# id|name|age
# 1|Alice|30

# Delete data
python -m minisqlite sample.db "DELETE FROM users WHERE id = 1;"
```

### Interactive Mode

```bash
python -m minisqlite sample.db

minisqlite> CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);
OK
minisqlite> INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);
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

## Test Command

```bash
# Run all tests
python3 -m unittest discover -s tests

# Compile check
python3 -m compileall -q minisqlite tests
```

## Architecture Notes

- **Schema Metadata**: Table definitions are stored as JSON in page 1 of the database file
- **Row Data**: Actual row data is stored in BTree pages as encoded records
- **Storage Engine**: Custom BTree implementation for efficient data storage and retrieval
- **SQL Parser**: Hand-written recursive descent parser for SQL syntax
- **Record Encoding**: Custom binary encoding for row data with type information

## Limitations

- Single-user access only (no locking or concurrency control)
- Limited SQL feature set (see "What is Intentionally Not Implemented")
- No error recovery for corrupted database files
- No backup or migration tools