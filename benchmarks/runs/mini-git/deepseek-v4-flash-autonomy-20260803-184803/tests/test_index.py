"""Tests for index persistence and safe path normalization."""

import json
import tempfile
import unittest
from pathlib import Path

from minigit.errors import InvalidPathError
from minigit.index import Index, normalize_path


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.git_dir = self.root / ".minigit"
        self.git_dir.mkdir()
        self.index = Index(self.git_dir)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_load_returns_empty_when_index_absent(self):
        self.index.load()
        self.assertEqual(self.index.entries(), {})

    def test_set_remove_and_save_round_trip(self):
        self.index.load()
        self.index.set("a.txt", "a" * 64)
        self.index.set("b.txt", "b" * 64)
        self.index.save()

        fresh = Index(self.git_dir)
        fresh.load()
        self.assertEqual(fresh.get("a.txt"), "a" * 64)
        self.assertEqual(fresh.get("b.txt"), "b" * 64)

        fresh.remove("a.txt")
        fresh.save()
        reloaded = Index(self.git_dir)
        reloaded.load()
        self.assertIsNone(reloaded.get("a.txt"))
        self.assertEqual(reloaded.get("b.txt"), "b" * 64)

    def test_index_json_is_utf8_sorted_compact(self):
        self.index.load()
        self.index.set("b.txt", "b" * 64)
        self.index.set("a.txt", "a" * 64)
        self.index.save()
        raw = (self.git_dir / "index.json").read_bytes()
        self.assertEqual(raw, json.dumps(
            {"a.txt": "a" * 64, "b.txt": "b" * 64},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"))

    def test_normalize_path_rejects_absolute(self):
        with self.assertRaises(InvalidPathError):
            normalize_path(self.root, str(self.root / "x.txt"))

    def test_normalize_path_rejects_escaping(self):
        with self.assertRaises(InvalidPathError):
            normalize_path(self.root, "../outside.txt")

    def test_normalize_path_rejects_minigit(self):
        with self.assertRaises(InvalidPathError):
            normalize_path(self.root, ".minigit/HEAD")

    def test_normalize_path_accepts_relative(self):
        self.assertEqual(normalize_path(self.root, "docs/readme.txt"), "docs/readme.txt")


if __name__ == "__main__":
    unittest.main()