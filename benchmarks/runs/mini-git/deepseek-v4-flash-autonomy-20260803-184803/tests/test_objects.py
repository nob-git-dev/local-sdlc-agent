"""Tests for content-addressed object storage."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from minigit.errors import CorruptObjectError
from minigit.objects import ObjectStore, hash_object


class ObjectStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.git_dir = Path(self.tempdir.name) / ".minigit"
        (self.git_dir / "objects").mkdir(parents=True)
        self.store = ObjectStore(self.git_dir)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_hash_object_matches_sha256_formula(self):
        payload = b"hello\x00binary\xff"
        expected = hashlib.sha256(b"blob\x00" + payload).hexdigest()
        self.assertEqual(hash_object("blob", payload), expected)

    def test_write_is_idempotent_and_returns_lowercase_oid(self):
        payload = b"hello\x00binary\xff"
        oid = self.store.write("blob", payload)
        self.assertRegex(oid, r"^[0-9a-f]{64}$")
        self.assertEqual(self.store.write("blob", payload), oid)

    def test_object_file_contains_encoded_bytes(self):
        payload = b"hello\x00binary\xff"
        oid = self.store.write("blob", payload)
        path = self.git_dir / "objects" / oid[:2] / oid[2:]
        self.assertEqual(path.read_bytes(), b"blob\x00" + payload)

    def test_read_round_trips_blob_bytes(self):
        payload = b"\x00\xff\x10"
        oid = self.store.write("blob", payload)
        self.assertEqual(self.store.read(oid), ("blob", payload))

    def test_read_detects_tampered_content(self):
        payload = b"hello\x00binary\xff"
        oid = self.store.write("blob", payload)
        path = self.git_dir / "objects" / oid[:2] / oid[2:]
        path.write_bytes(b"blob\x00tampered")
        with self.assertRaises(CorruptObjectError):
            self.store.read(oid)

    def test_read_detects_missing_object(self):
        with self.assertRaises(CorruptObjectError):
            self.store.read("0" * 64)

    def test_read_detects_malformed_object(self):
        oid = self.store.write("blob", b"payload")
        path = self.git_dir / "objects" / oid[:2] / oid[2:]
        path.write_bytes(b"not-an-object")
        with self.assertRaises(CorruptObjectError):
            self.store.read(oid)

    def test_tree_json_is_deterministic(self):
        tree = {"b.txt": "b" * 64, "a.txt": "a" * 64}
        payload = json.dumps(tree, sort_keys=True, separators=(",", ":")).encode("utf-8")
        oid = self.store.write("tree", payload)
        self.assertEqual(self.store.read(oid), ("tree", payload))
        self.assertEqual(self.store.read_json(oid), tree)

    def test_commit_json_is_deterministic(self):
        commit = {
            "tree": "t" * 64,
            "parent": None,
            "message": "initial",
            "author": "Ada",
            "timestamp": "2026-01-01T00:00:00Z",
        }
        payload = json.dumps(commit, sort_keys=True, separators=(",", ":")).encode("utf-8")
        oid = self.store.write("commit", payload)
        self.assertEqual(self.store.read(oid), ("commit", payload))
        self.assertEqual(self.store.read_json(oid), commit)

    def test_unsupported_kind_is_rejected(self):
        with self.assertRaises(CorruptObjectError):
            self.store.write("unknown", b"payload")


if __name__ == "__main__":
    unittest.main()
