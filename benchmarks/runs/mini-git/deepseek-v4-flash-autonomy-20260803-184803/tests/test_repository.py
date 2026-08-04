#!/usr/bin/env python3
"""Tests for repository initialization, add, and index persistence."""

import json
import tempfile
import unittest
from pathlib import Path

from minigit.errors import InvalidPathError, RepositoryNotFoundError
from minigit.repository import Repository


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "repo"
        self.root.mkdir()

    def tearDown(self):
        self.tempdir.cleanup()

    def write(self, path, data):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            target.write_bytes(data)
        else:
            target.write_text(data, encoding="utf-8")
        return target

    def test_init_creates_required_layout(self):
        repo = Repository.init(self.root)
        git_dir = self.root / ".minigit"
        self.assertEqual((git_dir / "HEAD").read_text(), "ref: refs/heads/main\n")
        self.assertTrue((git_dir / "refs" / "heads" / "main").exists())
        self.assertEqual((git_dir / "refs" / "heads" / "main").read_text(), "")
        self.assertEqual(json.loads((git_dir / "index.json").read_text()), {})
        self.assertTrue((git_dir / "objects").is_dir())
        self.assertEqual(repo.root, self.root)
        self.assertEqual(repo.git_dir, git_dir)

    def test_init_is_idempotent_and_preserves_data(self):
        repo = Repository.init(self.root)
        self.write("a.txt", "hello")
        repo.add(["a.txt"])
        index_before = (self.root / ".minigit" / "index.json").read_bytes()

        again = Repository.init(self.root)
        self.assertEqual((self.root / ".minigit" / "index.json").read_bytes(), index_before)
        self.assertEqual(again.root, self.root)
        self.assertEqual(again.git_dir, self.root / ".minigit")

    def test_add_dot_recursively_stages_and_excludes_minigit(self):
        repo = Repository.init(self.root)
        self.write("docs/readme.txt", "first\n")
        self.write("assets/raw.bin", b"\x00\xff\x10")
        self.write("nested/deep/file.txt", "deep")
        repo.add(["."])
        entries = repo.index.entries()
        self.assertEqual(
            sorted(entries),
            ["assets/raw.bin", "docs/readme.txt", "nested/deep/file.txt"],
        )
        self.assertNotIn(".minigit/HEAD", entries)
        self.assertNotIn(".minigit/index.json", entries)

    def test_binary_blob_round_trips_bytes(self):
        repo = Repository.init(self.root)
        payload = b"\x00\xff\x10\xfe"
        self.write("raw.bin", payload)
        repo.add(["raw.bin"])
        oid = repo.index.get("raw.bin")
        self.assertRegex(oid, r"^[0-9a-f]{64}$")
        kind, stored = repo.objects.read(oid)
        self.assertEqual(kind, "blob")
        self.assertEqual(stored, payload)

    def test_explicit_add_of_missing_tracked_path_stages_deletion(self):
        repo = Repository.init(self.root)
        self.write("gone.txt", "content")
        repo.add(["gone.txt"])
        self.assertIsNotNone(repo.index.get("gone.txt"))

        (self.root / "gone.txt").unlink()
        repo.add(["gone.txt"])
        self.assertIsNone(repo.index.get("gone.txt"))

    def test_invalid_path_add_does_not_change_index(self):
        repo = Repository.init(self.root)
        self.write("keep.txt", "keep")
        repo.add(["keep.txt"])
        index_before = (self.root / ".minigit" / "index.json").read_bytes()

        with self.assertRaises(InvalidPathError):
            repo.add(["keep.txt", "../outside.txt"])
        self.assertEqual((self.root / ".minigit" / "index.json").read_bytes(), index_before)

        with self.assertRaises(InvalidPathError):
            repo.add([str((self.root / "x.txt").resolve())])
        self.assertEqual((self.root / ".minigit" / "index.json").read_bytes(), index_before)

        with self.assertRaises(InvalidPathError):
            repo.add([".minigit/HEAD"])
        self.assertEqual((self.root / ".minigit" / "index.json").read_bytes(), index_before)

    def test_open_raises_repository_not_found_outside_repo(self):
        with self.assertRaises(RepositoryNotFoundError):
            Repository.open(self.root)

    def test_index_persists_across_reopen(self):
        repo = Repository.init(self.root)
        self.write("a.txt", "alpha")
        self.write("b.txt", "beta")
        repo.add(["a.txt", "b.txt"])

        reopened = Repository.open(self.root)
        reopened.index.load()
        self.assertEqual(reopened.index.get("a.txt"), repo.index.get("a.txt"))
        self.assertEqual(reopened.index.get("b.txt"), repo.index.get("b.txt"))


if __name__ == "__main__":
    unittest.main()
