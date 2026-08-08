import os
import subprocess
import sys
import tempfile
import unittest


class CliSingleTest(unittest.TestCase):
    """Tests for single-statement CLI execution (SPEC.md 4.1)."""

    def _run_cli(self, db_path, sql):
        return subprocess.run(
            [sys.executable, "-m", "minisqlite", db_path, sql],
            capture_output=True,
            text=True,
        )

    def test_single_sql_creates_and_selects(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "sample.db")
            create = self._run_cli(
                db_path,
                "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);",
            )
            self.assertEqual(create.returncode, 0)
            insert = self._run_cli(
                db_path,
                "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);",
            )
            self.assertEqual(insert.returncode, 0)
            select = self._run_cli(db_path, "SELECT * FROM users;")
            self.assertEqual(select.returncode, 0)
            self.assertEqual(select.stdout.strip(), "id|name|age\n1|Alice|30")

    def test_select_output_is_pipe_separated(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "sample.db")
            self._run_cli(
                db_path,
                "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);",
            )
            self._run_cli(
                db_path,
                "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);",
            )
            select = self._run_cli(db_path, "SELECT * FROM users;")
            self.assertEqual(select.stdout.splitlines()[0], "id|name|age")
            self.assertEqual(select.stdout.splitlines()[1], "1|Alice|30")

    def test_error_prints_error_and_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "sample.db")
            result = self._run_cli(db_path, "SELECT * FROM missing_table;")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Error:", result.stderr)

    def test_persistence_across_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "sample.db")
            self._run_cli(
                db_path,
                "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);",
            )
            self._run_cli(
                db_path,
                "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);",
            )
            select = self._run_cli(db_path, "SELECT * FROM users;")
            self.assertEqual(select.returncode, 0)
            self.assertEqual(select.stdout.strip(), "id|name|age\n1|Alice|30")

    def test_multiple_inserts_persist_across_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "sample.db")
            self._run_cli(
                db_path,
                "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);",
            )
            self._run_cli(
                db_path,
                "INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);",
            )
            self._run_cli(
                db_path,
                "INSERT INTO users (id, name, age) VALUES (2, 'Bob', 25);",
            )
            select = self._run_cli(db_path, "SELECT * FROM users;")
            self.assertEqual(select.returncode, 0)
            self.assertEqual(
                select.stdout.strip(),
                "id|name|age\n1|Alice|30\n2|Bob|25",
            )


if __name__ == "__main__":
    unittest.main()
