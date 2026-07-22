"""CLI entry point for Mini SQLite Engine.

Usage:
    python -m minisqlite <db_path> [sql]

If sql is provided, execute it and exit.
If sql is not provided, enter interactive mode.
"""

import sys

from minisqlite.connection import connect


def format_select_result(columns: list[str], rows: list[list[object]]) -> str:
    """Format a SELECT result as a pipe-delimited table."""
    lines = []
    lines.append("|".join(columns))
    for row in rows:
        lines.append("|".join(str(v) for v in row))
    return "\n".join(lines)


def execute_sql(conn, sql: str) -> None:
    """Execute a single SQL statement and print the result."""
    result = conn.execute(sql)

    if result.columns:
        # SELECT statement
        print(format_select_result(result.columns, result.rows))
    else:
        # DDL/DML statement
        print("OK")


def interactive_mode(conn) -> None:
    """Enter interactive mode with the database."""
    print("Mini SQLite Engine - Interactive Mode")
    print("Type .exit to quit, .tables to list tables, .schema <table> for schema")

    while True:
        try:
            user_input = input("minisqlite> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        user_input = user_input.strip()

        if not user_input:
            continue

        if user_input == ".exit":
            break
        elif user_input == ".tables":
            tables = conn.list_tables()
            for table in tables:
                print(table)
        elif user_input.startswith(".schema "):
            table_name = user_input[8:].strip()
            if not table_name:
                print("Usage: .schema <table_name>")
                continue
            try:
                schema = conn.get_schema(table_name)
                print(schema)
            except Exception as e:
                print(f"Error: {e}")
        else:
            try:
                execute_sql(conn, user_input)
            except Exception as e:
                print(f"Error: {e}")


def main():
    """Main entry point for the CLI."""
    if len(sys.argv) < 2:
        print("Usage: python -m minisqlite <db_path> [sql]")
        sys.exit(1)

    db_path = sys.argv[1]

    if len(sys.argv) == 2:
        # Interactive mode
        conn = connect(db_path)
        try:
            interactive_mode(conn)
        finally:
            conn.close()
    else:
        # Single SQL mode
        sql = sys.argv[2]
        conn = connect(db_path)
        try:
            execute_sql(conn, sql)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            conn.close()


if __name__ == "__main__":
    main()