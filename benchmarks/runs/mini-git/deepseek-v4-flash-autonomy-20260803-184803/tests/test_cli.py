#!/usr/bin/env python3
"""Tests for the minigit command-line interface in separate processes."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "repo"
        self.root.mkdir()

    def tearDown(self):
        self.tempdir.cleanup()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "minigit", "--repo", str(self.root), *args],
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
        )

    def write(self, path, data):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data, encoding="utf-8")
        return target

    def test_init_creates_layout_in_separate_process(self):
        result = self.run_cli("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / ".minigit" / "HEAD").read_text(), "ref: refs/heads/main\n")
        self.assertEqual((self.root / ".minigit" / "index.json").read_text(), "{}")

    def test_add_commit_status_log_round_trip(self):
        self.run_cli("init")
        self.write("a.txt", "hello")
        add_result = self.run_cli("add", "a.txt")
        self.assertEqual(add_result.returncode, 0, add_result.stderr)
        commit_result = self.run_cli("commit", "-m", "initial")
        self.assertEqual(commit_result.returncode, 0, commit_result.stderr)
        oid = commit_result.stdout.strip()
        self.assertRegex(oid, r"^[0-9a-f]{64}$")
        status_result = self.run_cli("status", "--porcelain")
        self.assertEqual(status_result.returncode, 0, status_result.stderr)
        self.assertEqual(status_result.stdout, "")
        log_result = self.run_cli("log", "--oneline")
        self.assertEqual(log_result.returncode, 0, log_result.stderr)
        self.assertEqual(log_result.stdout, f"{oid[:12]} initial\n")

    def test_commit_prints_only_oid(self):
        self.run_cli("init")
        self.write("a.txt", "hello")
        self.run_cli("add", "a.txt")
        result = self.run_cli("commit", "-m", "initial")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"^[0-9a-f]{64}\n$")

    def test_porcelain_status_formats_lines(self):
        self.run_cli("init")
        self.write("tracked.txt", "one")
        self.run_cli("add", "tracked.txt")
        self.run_cli("commit", "-m", "initial")
        self.write("tracked.txt", "two")
        self.write("new.txt", "new")
        result = self.run_cli("status", "--porcelain")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertIn(" M tracked.txt", lines)
        self.assertIn("?? new.txt", lines)

    def test_domain_error_exits_nonzero_without_traceback(self):
        result = self.run_cli("status")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("minigit:", result.stderr)

    def test_checkout_restores_earlier_tree(self):
        self.run_cli("init")
        self.write("app.txt", "one")
        self.run_cli("add", "app.txt")
        first = self.run_cli("commit", "-m", "one")
        self.write("app.txt", "two")
        self.run_cli("add", "app.txt")
        second = self.run_cli("commit", "-m", "two")
        checkout_result = self.run_cli("checkout", first.stdout.strip())
        self.assertEqual(checkout_result.returncode, 0, checkout_result.stderr)
        self.assertEqual((self.root / "app.txt").read_text(), "one")


if __name__ == "__main__":
    unittest.main()
