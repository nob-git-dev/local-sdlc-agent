#!/usr/bin/env python3
"""Tests for commit graph, references, and revision resolution."""

import tempfile
import unittest
from pathlib import Path

from minigit.errors import NothingToCommitError, RevisionNotFoundError
from minigit.repository import Repository


class CommitGraphTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "repo"
        self.root.mkdir()

    def tearDown(self):
        self.tempdir.cleanup()

    def write(self, path, data):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data, encoding="utf-8")
        return target

    def test_first_commit_has_null_parent_and_advances_main(self):
        repo = Repository.init(self.root)
        self.write("a.txt", "one")
        repo.add(["a.txt"])
        first = repo.commit("initial", author="Ada", timestamp="2026-01-01T00:00:00Z")
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertEqual(repo.head_commit, first)
        self.assertEqual(repo.refs.read_branch("main"), first)
        entry = repo.log()[0]
        self.assertEqual(entry["oid"], first)
        self.assertEqual(entry["parent"], None)
        self.assertEqual(entry["message"], "initial")
        self.assertEqual(entry["author"], "Ada")
        self.assertEqual(entry["timestamp"], "2026-01-01T00:00:00Z")

    def test_second_commit_forms_parent_chain(self):
        repo = Repository.init(self.root)
        self.write("app.txt", "one")
        repo.add(["app.txt"])
        first = repo.commit("one", timestamp="1")
        self.write("app.txt", "two")
        repo.add(["app.txt"])
        second = repo.commit("two", timestamp="2")
        history = repo.log()
        self.assertEqual([item["oid"] for item in history], [second, first])
        self.assertEqual(history[0]["parent"], first)
        self.assertEqual(history[1]["parent"], None)

    def test_commit_persists_across_reopen(self):
        repo = Repository.init(self.root)
        self.write("a.txt", "alpha")
        repo.add(["a.txt"])
        first = repo.commit("initial", timestamp="2026-01-01T00:00:00Z")
        reopened = Repository.open(self.root)
        self.assertEqual(reopened.head_commit, first)
        entry = reopened.log()[0]
        self.assertEqual(entry["oid"], first)
        self.assertEqual(entry["parent"], None)
        self.assertEqual(entry["message"], "initial")
        self.assertEqual(entry["timestamp"], "2026-01-01T00:00:00Z")

    def test_no_change_commit_raises_and_does_not_move_head(self):
        repo = Repository.init(self.root)
        self.write("app.txt", "one")
        repo.add(["app.txt"])
        first = repo.commit("one", timestamp="1")
        before = repo.head_commit
        with self.assertRaises(NothingToCommitError):
            repo.commit("no changes", timestamp="2")
        self.assertEqual(repo.head_commit, before)

    def test_empty_message_is_rejected(self):
        from minigit.errors import MiniGitError

        repo = Repository.init(self.root)
        self.write("a.txt", "one")
        repo.add(["a.txt"])
        with self.assertRaises(MiniGitError):
            repo.commit("   ", timestamp="1")

    def test_revision_resolution_handles_head_branch_oid_and_prefix(self):
        repo = Repository.init(self.root)
        self.write("app.txt", "one")
        repo.add(["app.txt"])
        first = repo.commit("one", timestamp="1")
        self.write("app.txt", "two")
        repo.add(["app.txt"])
        second = repo.commit("two", timestamp="2")
        self.assertEqual(repo.resolve_revision("HEAD"), second)
        self.assertEqual(repo.resolve_revision("main"), second)
        self.assertEqual(repo.resolve_revision(first), first)
        self.assertEqual(repo.resolve_revision(first[:16]), first)
        with self.assertRaises(RevisionNotFoundError):
            repo.resolve_revision("deadbeef")

    def test_log_max_count_limits_history(self):
        repo = Repository.init(self.root)
        self.write("app.txt", "one")
        repo.add(["app.txt"])
        first = repo.commit("one", timestamp="1")
        self.write("app.txt", "two")
        repo.add(["app.txt"])
        second = repo.commit("two", timestamp="2")
        limited = repo.log(max_count=1)
        self.assertEqual([item["oid"] for item in limited], [second])


if __name__ == "__main__":
    unittest.main()
