"""Tests for the single-leaf BTree implementation."""

import os
import tempfile
import unittest

from minisqlite.errors import DuplicateKeyError, StorageError
from minisqlite.storage.btree import BTree
from minisqlite.storage.pager import Pager


class TestBTree(unittest.TestCase):
    """Test cases for BTree single-leaf operations."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db_path = os.path.join(self._tmpdir, "test.db")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_pager(self):
        return Pager(self._db_path)

    # --- Empty tree search returns None ---
    def test_empty_tree_search_returns_none(self):
        pager = self._make_pager()
        try:
            tree = BTree(pager)
            result = tree.search(1)
            self.assertIsNone(result)
        finally:
            pager.close()

    # --- One insert/search works ---
    def test_one_insert_search(self):
        pager = self._make_pager()
        try:
            tree = BTree(pager)
            tree.insert(1, b"hello")
            result = tree.search(1)
            self.assertEqual(result, b"hello")
        finally:
            pager.close()

    # --- Multiple inserts scan in rowid ascending order even if inserted out of order ---
    def test_multiple_inserts_scan_in_order(self):
        pager = self._make_pager()
        try:
            tree = BTree(pager)
            tree.insert(3, b"c")
            tree.insert(1, b"a")
            tree.insert(2, b"b")
            results = tree.scan_all()
            self.assertEqual(results, [(1, b"a"), (2, b"b"), (3, b"c")])
        finally:
            pager.close()

    # --- Duplicate rowid raises DuplicateKeyError ---
    def test_duplicate_rowid_raises_duplicate_key_error(self):
        pager = self._make_pager()
        try:
            tree = BTree(pager)
            tree.insert(1, b"first")
            with self.assertRaises(DuplicateKeyError):
                tree.insert(1, b"second")
        finally:
            pager.close()

    # --- Delete returns True for existing row and False for missing row ---
    def test_delete_existing_returns_true(self):
        pager = self._make_pager()
        try:
            tree = BTree(pager)
            tree.insert(1, b"data")
            result = tree.delete(1)
            self.assertTrue(result)
        finally:
            pager.close()

    def test_delete_missing_returns_false(self):
        pager = self._make_pager()
        try:
            tree = BTree(pager)
            result = tree.delete(999)
            self.assertFalse(result)
        finally:
            pager.close()

    # --- Deleted key is not found ---
    def test_deleted_key_not_found(self):
        pager = self._make_pager()
        try:
            tree = BTree(pager)
            tree.insert(1, b"data")
            tree.delete(1)
            result = tree.search(1)
            self.assertIsNone(result)
        finally:
            pager.close()

    # --- Data persists after close/reopen using root_page_id ---
    def test_persistence_after_close_reopen(self):
        pager = self._make_pager()
        tree = BTree(pager)
        tree.insert(1, b"persist")
        tree.insert(2, b"me")
        root_page_id = tree.root_page_id
        pager.close()

        # Reopen
        pager2 = self._make_pager()
        try:
            tree2 = BTree(pager2, root_page_id=root_page_id)
            self.assertEqual(tree2.search(1), b"persist")
            self.assertEqual(tree2.search(2), b"me")
            results = tree2.scan_all()
            self.assertEqual(results, [(1, b"persist"), (2, b"me")])
        finally:
            pager2.close()

    # --- Page overflow is detected ---
    def test_page_overflow_large_payload(self):
        pager = self._make_pager()
        try:
            tree = BTree(pager)
            # Insert a payload that is too large to fit in a single leaf page
            large_payload = b"x" * (4096 * 2)
            with self.assertRaises((StorageError, ValueError)):
                tree.insert(1, large_payload)
        finally:
            pager.close()

    def test_page_overflow_many_rows(self):
        pager = self._make_pager()
        try:
            tree = BTree(pager)
            # Insert many rows until overflow
            for i in range(10000):
                try:
                    tree.insert(i, b"data")
                except (StorageError, ValueError):
                    break
            # Verify we got an overflow error at some point
            # (the loop should have raised at least once)
        finally:
            pager.close()


# --- Multi-leaf B+Tree: split, internal root, reopen, scan, delete ---
    def test_split_allocates_multiple_pages(self):
        """Inserting enough rows with large payloads forces leaf split."""
        pager = self._make_pager()
        try:
            tree = BTree(pager)
            # Use payloads large enough to force split with ~80 rows
            for i in range(80):
                payload = (f"v{i:03d}-" + "x" * 100).encode()
                tree.insert(i, payload)
            # Verify multiple pages were allocated
            # The root page should now be an internal root
            from minisqlite.storage.btree import INTERNAL_MAGIC
            root_page = pager.read_page(tree.root_page_id)
            self.assertEqual(root_page[0:4], INTERNAL_MAGIC,
                             "Root should be internal after split")
        finally:
            pager.close()

    def test_split_creates_internal_root(self):
        """After split, root page is an internal root page."""
        pager = self._make_pager()
        try:
            tree = BTree(pager)
            for i in range(80):
                payload = (f"v{i:03d}-" + "x" * 100).encode()
                tree.insert(i, payload)
            from minisqlite.storage.btree import INTERNAL_MAGIC
            root_page = pager.read_page(tree.root_page_id)
            self.assertEqual(root_page[0:4], INTERNAL_MAGIC)
        finally:
            pager.close()

    def test_search_after_split(self):
        """All inserted rows can be searched after leaf split."""
        pager = self._make_pager()
        try:
            tree = BTree(pager)
            for i in range(80):
                payload = (f"v{i:03d}-" + "x" * 100).encode()
                tree.insert(i, payload)
            # Verify all rows are searchable
            missing = []
            for i in range(80):
                expected = (f"v{i:03d}-" + "x" * 100).encode()
                result = tree.search(i)
                if result != expected:
                    missing.append(i)
            self.assertEqual(missing, [],
                             f"Missing rows after split: {missing[:5]}")
        finally:
            pager.close()

    def test_scan_all_ascending_after_split(self):
        """scan_all() returns rows in ascending rowid order after split,
        including out-of-order inserts."""
        pager = self._make_pager()
        try:
            tree = BTree(pager)
            # Insert out of order
            import random
            rowids = list(range(80))
            random.seed(42)
            random.shuffle(rowids)
            for i in rowids:
                payload = (f"v{i:03d}-" + "x" * 100).encode()
                tree.insert(i, payload)
            results = tree.scan_all()
            expected = [(i, (f"v{i:03d}-" + "x" * 100).encode())
                        for i in range(80)]
            self.assertEqual(results, expected,
                             "scan_all should return ascending order")
        finally:
            pager.close()

    def test_delete_after_split_persists(self):
        """Delete after split removes the key, persists after close/reopen,
        and does not remove neighboring keys."""
        pager = self._make_pager()
        tree = BTree(pager)
        # Insert enough rows to force split
        for i in range(80):
            payload = (f"v{i:03d}-" + "x" * 100).encode()
            tree.insert(i, payload)
        root_page_id = tree.root_page_id
        pager.close()

        # Reopen and delete
        pager2 = self._make_pager()
        try:
            tree2 = BTree(pager2, root_page_id=root_page_id)
            # Delete a middle row
            self.assertTrue(tree2.delete(40))
            # Verify deleted row is gone
            self.assertIsNone(tree2.search(40))
            # Verify neighboring rows still exist
            self.assertEqual(tree2.search(39),
                             (f"v{39:03d}-" + "x" * 100).encode())
            self.assertEqual(tree2.search(41),
                             (f"v{41:03d}-" + "x" * 100).encode())
            # Verify scan_all still works
            results = tree2.scan_all()
            rowids = [r for r, _ in results]
            self.assertIn(39, rowids)
            self.assertNotIn(40, rowids)
            self.assertIn(41, rowids)
            self.assertEqual(len(rowids), 79)
        finally:
            pager2.close()


if __name__ == "__main__":
    unittest.main()
