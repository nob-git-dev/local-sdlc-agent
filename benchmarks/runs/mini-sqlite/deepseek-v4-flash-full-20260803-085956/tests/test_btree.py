import os
import struct
import tempfile
import unittest

from minisqlite.errors import DuplicateKeyError
from minisqlite.storage.btree import BTree
from minisqlite.storage.pager import Pager


class BTreeS06Test(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._path = os.path.join(self._tmpdir.name, "test.db")
        self._pager = Pager(self._path)
        self._tree = BTree(self._pager)

    def tearDown(self):
        self._pager.close()
        self._tmpdir.cleanup()

    def test_empty_search_returns_none(self):
        self.assertIsNone(self._tree.search(1))

    def test_insert_and_search_single_row(self):
        self._tree.insert(42, b"hello")
        self.assertEqual(self._tree.search(42), b"hello")

    def test_insert_and_search_multiple_rows(self):
        self._tree.insert(1, b"a")
        self._tree.insert(2, b"b")
        self._tree.insert(3, b"c")
        self.assertEqual(self._tree.search(1), b"a")
        self.assertEqual(self._tree.search(2), b"b")
        self.assertEqual(self._tree.search(3), b"c")

    def test_scan_all_sorted_after_out_of_order_insert(self):
        self._tree.insert(3, b"c")
        self._tree.insert(1, b"a")
        self._tree.insert(2, b"b")
        self.assertEqual(
            list(self._tree.scan_all()),
            [(1, b"a"), (2, b"b"), (3, b"c")],
        )

    def test_duplicate_insert_raises_duplicate_key_error(self):
        self._tree.insert(7, b"first")
        with self.assertRaises(DuplicateKeyError):
            self._tree.insert(7, b"second")

    def test_delete_existing_returns_true(self):
        self._tree.insert(5, b"payload")
        self.assertTrue(self._tree.delete(5))

    def test_delete_missing_returns_false(self):
        self.assertFalse(self._tree.delete(999))

    def test_delete_then_search_returns_none(self):
        self._tree.insert(5, b"payload")
        self.assertTrue(self._tree.delete(5))
        self.assertIsNone(self._tree.search(5))

    def test_reopen_using_root_page_id(self):
        self._tree.insert(10, b"ten")
        self._tree.insert(20, b"twenty")
        root_page_id = self._tree.root_page_id
        self._pager.close()

        self._pager = Pager(self._path)
        reopened = BTree(self._pager, root_page_id=root_page_id)
        self.assertEqual(reopened.search(10), b"ten")
        self.assertEqual(reopened.search(20), b"twenty")
        self.assertEqual(
            list(reopened.scan_all()),
            [(10, b"ten"), (20, b"twenty")],
        )

    def test_oversized_payload_rejected(self):
        oversized = b"x" * 4096
        with self.assertRaises(ValueError):
            self._tree.insert(1, oversized)

    def test_signed_rowid_boundaries(self):
        min_rowid = -(2**63)
        max_rowid = 2**63 - 1
        self._tree.insert(min_rowid, b"min")
        self._tree.insert(max_rowid, b"max")
        self.assertEqual(self._tree.search(min_rowid), b"min")
        self.assertEqual(self._tree.search(max_rowid), b"max")
        self.assertEqual(
            list(self._tree.scan_all()),
            [(min_rowid, b"min"), (max_rowid, b"max")],
        )


if __name__ == "__main__":
    unittest.main()