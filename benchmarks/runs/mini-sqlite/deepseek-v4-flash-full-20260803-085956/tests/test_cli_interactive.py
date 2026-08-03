import io
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr

from minisqlite.cli import main


class CliInteractiveTest(unittest.TestCase):
    def _run_interactive(self, input_text):
        """Run the CLI in interactive mode with the given stdin text.

        Returns (exit_code, stdout_text).
        """
        stdin = io.StringIO(input_text)
        stdout = io.StringIO()
        stderr = io.StringIO()
        old_stdin = sys.stdin
        sys.stdin = stdin
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main([":memory:"])
        finally:
            sys.stdin = old_stdin
        return code, stdout.getvalue()

    def test_multiple_sql_statements(self):
        code, out = self._run_interactive(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);\n"
            "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);\n"
            "INSERT INTO users (id, name, age) VALUES (2, 'Bob', 25);\n"
            "SELECT * FROM users;\n"
        )
        self.assertEqual(code, 0)
        self.assertIn("1|Alice|30", out)
        self.assertIn("2|Bob|25", out)

    def test_dot_tables(self):
        code, out = self._run_interactive(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);\n"
            "CREATE TABLE posts (id INTEGER PRIMARY KEY, title TEXT);\n"
            ".tables\n"
        )
        self.assertEqual(code, 0)
        self.assertIn("users", out)
        self.assertIn("posts", out)

    def test_dot_schema_users(self):
        code, out = self._run_interactive(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);\n"
            ".schema users\n"
        )
        self.assertEqual(code, 0)
        self.assertIn("CREATE TABLE users", out)
        self.assertIn("id INTEGER PRIMARY KEY", out)

    def test_dot_exit(self):
        code, out = self._run_interactive(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);\n"
            ".exit\n"
        )
        self.assertEqual(code, 0)

    def test_error_then_continue(self):
        code, out = self._run_interactive(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);\n"
            "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);\n"
            "SELECT * FROM missing_table;\n"
            "SELECT * FROM users;\n"
        )
        self.assertEqual(code, 0)
        self.assertIn("ERROR:", out)
        self.assertIn("1|Alice|30", out)


if __name__ == "__main__":
    unittest.main()
