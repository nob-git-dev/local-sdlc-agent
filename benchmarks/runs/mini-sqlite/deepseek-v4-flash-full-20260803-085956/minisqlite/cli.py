"""Command-line interface for MiniSQLite Engine.

This module implements the CLI described in SPEC.md section 4.1 and
4.2.  It supports single-statement execution and interactive mode with
the dot commands .tables, .schema, and .exit.  Errors are printed as
ERROR: <message> and the process exits with a non-zero status.
"""

import sys

from minisqlite.connection import connect
from minisqlite.errors import MiniSQLiteError


def _format_result(result):
    """Format a Result as pipe-separated text lines."""
    lines = []
    lines.append("|".join(result.columns))
    for row in result.rows:
        lines.append("|".join(str(value) for value in row))
    return "\n".join(lines)


def _print_result(result):
    """Print a Result to stdout."""
    if result.columns:
        print(_format_result(result))
    elif result.rows_affected:
        print("deleted: %d" % result.rows_affected)


def _run_statement(conn, sql):
    """Execute a single SQL statement and print its output."""
    result = conn.execute(sql)
    statement_type = sql.strip().lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    if statement_type == "SELECT":
        _print_result(result)
    elif statement_type == "DELETE":
        print("deleted: %d" % result.rows_affected)
    else:
        print("OK")


def _run_dot_command(conn, command):
    """Handle a dot command.  Returns True if the session should exit."""
    parts = command.strip().split()
    if not parts:
        return False
    name = parts[0].lower()
    if name == ".exit":
        return True
    if name == ".tables":
        table_names = conn._schema.table_names()
        if table_names:
            print(" ".join(table_names))
        return False
    if name == ".schema":
        if len(parts) < 2:
            print("ERROR: usage: .schema table_name")
            return False
        table_name = parts[1]
        table = conn._schema.get_table(table_name)
        if table is None:
            print("ERROR: no such table: %s" % table_name)
            return False
        print(table.sql)
        return False
    print("ERROR: unknown command: %s" % parts[0])
    return False


def _run_interactive(conn):
    """Run the interactive REPL loop."""
    while True:
        try:
            line = input("minisqlite> ")
        except EOFError:
            break
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("."):
            if _run_dot_command(conn, stripped):
                break
            continue
        try:
            _run_statement(conn, stripped)
        except MiniSQLiteError as exc:
            print("ERROR: %s" % exc)
        except Exception as exc:  # pragma: no cover - defensive guard
            print("ERROR: %s" % exc)


def main(argv=None):
    """Run the CLI.  Returns the process exit code."""
    if argv is None:
        argv = sys.argv[1:]
    if len(argv) < 1:
        print("usage: python -m minisqlite <database> [sql]", file=sys.stderr)
        return 2
    db_path = argv[0]
    conn = connect(db_path)
    try:
        if len(argv) >= 2:
            sql = " ".join(argv[1:])
            try:
                _run_statement(conn, sql)
            except MiniSQLiteError as exc:
                print("Error: %s" % exc, file=sys.stderr)
                return 1
            return 0
        _run_interactive(conn)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())