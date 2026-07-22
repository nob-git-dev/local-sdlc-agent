"""Tests for the CLI layer (Stage 4)."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest


class TestCLI(unittest.TestCase):
    """Test cases for the CLI interface."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db_path = os.path.join(self._tmpdir, "test.db")

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _run_cli(self, *args: str) -> subprocess.CompletedProcess:
        """Run the CLI with the given arguments."""
        cmd = [
            sys.executable,
            "-m",
            "minisqlite",
            self._db_path,
            *args,
        ]
        return subprocess.run(cmd, capture_output=True, text=True)

    def test_create_table_exits_0(self):
        """CREATE TABLE exits with code 0."""
        result = self._run_cli("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);")
        self.assertEqual(result.returncode, 0)
        self.assertIn("OK", result.stdout)

    def test_insert_and_select(self):
        """INSERT and SELECT work correctly through CLI."""
        # Create table
        result = self._run_cli("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);")
        self.assertEqual(result.returncode, 0)

        # Insert data
        result = self._run_cli("INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);")
        self.assertEqual(result.returncode, 0)

        # Select data
        result = self._run_cli("SELECT * FROM users;")
        self.assertEqual(result.returncode, 0)
        self.assertIn("id|name|age", result.stdout)
        self.assertIn("1|Alice|30", result.stdout)

    def test_select_with_where(self):
        """SELECT with WHERE clause works correctly."""
        # Create table
        self._run_cli("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);")

        # Insert data
        self._run_cli("INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);")
        self._run_cli("INSERT INTO users (id, name, age) VALUES (2, 'Bob', 40);")

        # Select with WHERE
        result = self._run_cli("SELECT name FROM users WHERE id = 1;")
        self.assertEqual(result.returncode, 0)
        self.assertIn("name", result.stdout)
        self.assertIn("Alice", result.stdout)
        self.assertNotIn("Bob", result.stdout)

    def test_delete_row(self):
        """DELETE removes row correctly."""
        # Create table and insert data
        self._run_cli("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);")
        self._run_cli("INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);")
        self._run_cli("INSERT INTO users (id, name, age) VALUES (2, 'Bob', 40);")

        # Delete Alice
        result = self._run_cli("DELETE FROM users WHERE id = 1;")
        self.assertEqual(result.returncode, 0)

        # Verify deletion
        result = self._run_cli("SELECT * FROM users;")
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("Alice", result.stdout)
        self.assertIn("Bob", result.stdout)

    def test_persistence_across_invocations(self):
        """Data persists across multiple CLI invocations."""
        # Create table
        self._run_cli("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);")

        # Insert data
        self._run_cli("INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);")

        # Reopen and select
        result = self._run_cli("SELECT * FROM users;")
        self.assertEqual(result.returncode, 0)
        self.assertIn("id|name|age", result.stdout)
        self.assertIn("1|Alice|30", result.stdout)

    def test_interactive_mode_tables(self):
        """Interactive mode .tables command works."""
        # Create table and insert data
        self._run_cli("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);")
        self._run_cli("INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30);")

        # Interactive mode with .tables
        cmd = [
            sys.executable,
            "-m",
            "minisqlite",
            self._db_path,
        ]
        result = subprocess.run(
            cmd,
            input=".tables\n.exit\n",
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("users", result.stdout)

    def test_interactive_mode_schema(self):
        """Interactive mode .schema command works."""
        # Create table
        self._run_cli("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);")

        # Interactive mode with .schema
        cmd = [
            sys.executable,
            "-m",
            "minisqlite",
            self._db_path,
        ]
        result = subprocess.run(
            cmd,
            input=".schema users\n.exit\n",
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("CREATE TABLE users", result.stdout)
        self.assertIn("id INTEGER PRIMARY KEY", result.stdout)
        self.assertIn("name TEXT", result.stdout)
        self.assertIn("age INTEGER", result.stdout)

    def test_invalid_sql_returns_error(self):
        """Invalid SQL returns error and non-zero exit code."""
        result = self._run_cli("INVALID SQL;")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Error", result.stderr)


if __name__ == "__main__":
    import sys
    unittest.main()