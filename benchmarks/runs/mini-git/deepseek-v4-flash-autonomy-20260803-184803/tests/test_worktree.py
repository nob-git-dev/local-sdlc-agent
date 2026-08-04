#!/usr/bin/env python3
"""Tests for status classification and safe checkout."""

import tempfile
import unittest
from pathlib import Path

from minigit.errors import DirtyWorktreeError
from minigit.repository import Repository


class WorktreeTests(unittest.TestCase):
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

    def test_status_clean_after_commit(self):
        repo = Repository.init(self.root)
        self.write("a.txt", "one")
        repo.add(["a.txt"])
        repo.commit("initial", timestamp="1")
        status = repo.status()
        self.assertEqual(status, {"staged": [], "modified": [], "deleted": [], "untracked": []})

    def test_status_distinguishes_all_four_keys(self):
        repo = Repository.init(self.root)
        self.write("tracked.txt", "one")
        self.write("untracked.txt", "new")
        repo.add(["tracked.txt"])
        repo.commit("initial", timestamp="1")

        self.write("tracked.txt", "two")
        self.write("staged.txt", "staged")
        repo.add(["staged.txt"])
        (self.root / "untracked.txt").unlink()
        self.write("untracked.txt", "again")
        (self.root / "tracked.txt").unlink()

        status = repo.status()
        self.assertEqual(status["staged"], ["staged.txt"])
        self.assertEqual(status["modified"], [])
        self.assertEqual(status["deleted"], ["tracked.txt"])
        self.assertEqual(status["untracked"], ["untracked.txt"])

    def test_status_never_reports_minigit(self):
        repo = Repository.init(self.root)
        self.write("a.txt", "one")
        repo.add(["a.txt"])
        repo.commit("initial", timestamp="1")
        status = repo.status()
        for key in status:
            self.assertNotIn(".minigit", " ".join(status[key]))

    def test_checkout_restores_earlier_tree(self):
        repo = Repository.init(self.root)
        self.write("app.txt", "one")
        repo.add(["app.txt"])
        first = repo.commit("one", timestamp="1")
        self.write("app.txt", "two")
        repo.add(["app.txt"])
        second = repo.commit("two", timestamp="2")

        result = repo.checkout(first)
        self.assertEqual(result, first)
        self.assertEqual(repo.head_commit, first)
        self.assertEqual((self.root / "app.txt").read_text(), "one")

    def test_checkout_removes_paths_absent_from_target(self):
        repo = Repository.init(self.root)
        self.write("keep.txt", "keep")
        self.write("remove.txt", "remove")
        repo.add(["keep.txt", "remove.txt"])
        first = repo.commit("initial", timestamp="1")
        (self.root / "remove.txt").unlink()
        repo.add(["remove.txt"])
        second = repo.commit("second", timestamp="2")

        repo.checkout(first)
        self.assertTrue((self.root / "keep.txt").exists())
        self.assertTrue((self.root / "remove.txt").exists())

    def test_checkout_preserves_untracked_files(self):
        repo = Repository.init(self.root)
        self.write("tracked.txt", "one")
        self.write("untracked.txt", "mine")
        repo.add(["tracked.txt"])
        first = repo.commit("initial", timestamp="1")
        self.write("tracked.txt", "two")
        repo.add(["tracked.txt"])
        second = repo.commit("second", timestamp="2")

        repo.checkout(first)
        self.assertEqual((self.root / "untracked.txt").read_text(), "mine")

    def test_checkout_refuses_dirty_worktree_unless_forced(self):
        repo = Repository.init(self.root)
        self.write("app.txt", "one")
        repo.add(["app.txt"])
        first = repo.commit("one", timestamp="1")
        self.write("app.txt", "two")
        repo.add(["app.txt"])
        second = repo.commit("two", timestamp="2")

        self.write("app.txt", "dirty")
        with self.assertRaises(DirtyWorktreeError):
            repo.checkout(first)
        self.assertEqual((self.root / "app.txt").read_text(), "dirty")

        result = repo.checkout(first, force=True)
        self.assertEqual(result, first)
        self.assertEqual((self.root / "app.txt").read_text(), "one")

    def test_checkout_detaches_head_on_oid(self):
        repo = Repository.init(self.root)
        self.write("app.txt", "one")
        repo.add(["app.txt"])
        first = repo.commit("one", timestamp="1")
        self.write("app.txt", "two")
        repo.add(["app.txt"])
        second = repo.commit("two", timestamp="2")

        repo.checkout(first)
        self.assertTrue(repo.refs.head_is_detached())
        self.assertEqual(repo.head_commit, first)

    def test_checkout_branch_stores_symbolic_head(self):
        repo = Repository.init(self.root)
        self.write("app.txt", "one")
        repo.add(["app.txt"])
        first = repo.commit("one", timestamp="1")
        self.write("app.txt", "two")
        repo.add(["app.txt"])
        second = repo.commit("two", timestamp="2")

        repo.checkout("main")
        self.assertFalse(repo.refs.head_is_detached())
        self.assertEqual(repo.head_commit, second)

    def test_checkout_restores_exact_binary_bytes(self):
        repo = Repository.init(self.root)
        payload = b"\x00\xff\x10\xfe"
        self.write("raw.bin", payload)
        repo.add(["raw.bin"])
        first = repo.commit("initial", timestamp="1")
        self.write("raw.bin", b"changed")
        repo.add(["raw.bin"])
        second = repo.commit("second", timestamp="2")

        repo.checkout(first)
        self.assertEqual((self.root / "raw.bin").read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
