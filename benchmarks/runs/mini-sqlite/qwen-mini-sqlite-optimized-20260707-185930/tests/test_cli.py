"""
CLI Tests

Tests for the CLI module to ensure correct command-line interface behavior.
"""

import unittest
import io
import sys
from unittest.mock import patch, MagicMock

from minisqlite.cli import format_result, handle_special_command
from minisqlite.result import Result
from minisqlite.errors import MiniSQLiteError


class TestFormatResult(unittest.TestCase):
    """Test result formatting for CLI output."""

    def test_format_result_with_columns_and_rows(self):
        """Test formatting a result with columns and rows."""
        result = Result(
            columns=["id", "name", "age"],
            rows=[[1, "Alice", 30], [2, "Bob", 25]]
        )
        output = format_result(result)
        
        lines = output.split("\n")
        self.assertEqual(lines[0], "id|name|age")
        self.assertEqual(lines[1], "-------------")  # separator line matches header length
        self.assertEqual(lines[2], "1|Alice|30")
        self.assertEqual(lines[3], "2|Bob|25")

    def test_format_result_with_only_columns(self):
        """Test formatting a result with only columns (no rows)."""
        result = Result(columns=["id", "name"])
        output = format_result(result)
        
        lines = output.split("\n")
        self.assertEqual(lines[0], "id|name")
        self.assertEqual(lines[1], "id|name")  # separator

    def test_format_result_empty(self):
        """Test formatting an empty result."""
        result = Result()
        output = format_result(result)
        self.assertEqual(output, "")

    def test_format_result_with_special_characters(self):
        """Test formatting with special characters in values."""
        result = Result(
            columns=["name"],
            rows=[["It's OK"], ["Hello\nWorld"]]
        )
        output = format_result(result)
        self.assertIn("It's OK", output)


class TestSpecialCommands(unittest.TestCase):
    """Test special CLI command handling."""

    def test_exit_command(self):
        """Test that .exit is recognized."""
        self.assertTrue(handle_special_command(".exit"))

    def test_quit_command(self):
        """Test that .quit is recognized."""
        self.assertTrue(handle_special_command(".quit"))

    def test_unknown_command(self):
        """Test that unknown commands are not handled."""
        self.assertFalse(handle_special_command(".unknown"))

    def test_case_insensitive(self):
        """Test that commands are case-insensitive."""
        self.assertTrue(handle_special_command(".EXIT"))
        self.assertTrue(handle_special_command(".Exit"))
        self.assertTrue(handle_special_command(".QUIT"))


class TestCLIIntegration(unittest.TestCase):
    """Integration tests for CLI functionality."""

    def test_single_sql_execution(self):
        """Test single SQL execution via CLI."""
        from minisqlite.cli import run_single_sql
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            
            # Create table
            exit_code = run_single_sql(db_path, "CREATE TABLE test (id INTEGER);")
            self.assertEqual(exit_code, 0)
            
            # Insert data
            exit_code = run_single_sql(db_path, "INSERT INTO test (id) VALUES (1);")
            self.assertEqual(exit_code, 0)
            
            # Select data
            exit_code = run_single_sql(db_path, "SELECT * FROM test;")
            self.assertEqual(exit_code, 0)

    def test_error_handling(self):
            """Test error handling in CLI."""
            from minisqlite.cli import run_single_sql
            import tempfile
            import os

            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = os.path.join(tmpdir, "test.db")

                # Try to select from non-existent table
                # The CLI prints "ERROR: ..." to stdout and returns exit code 1
                # We capture stdout to verify the error message is printed
                import io
                from contextlib import redirect_stdout

                f = io.StringIO()
                with redirect_stdout(f):
                    exit_code = run_single_sql(db_path, "SELECT * FROM nonexistent;")

                output = f.getvalue()
                self.assertIn("ERROR", output)  # Should print error message
                self.assertNotEqual(exit_code, 0)  # Should return error code
if __name__ == "__main__":
    unittest.main()