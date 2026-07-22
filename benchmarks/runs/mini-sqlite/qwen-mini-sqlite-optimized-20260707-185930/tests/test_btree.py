"""
B+Tree Storage Tests

Tests for the B+Tree storage layer to ensure correct leaf page operations.
"""

import os
import tempfile
import unittest

from minisqlite.errors import CorruptDatabaseError, DuplicateKeyError, StorageError
from minisqlite.storage.btree import BPlusTree, PAGE_TYPE_LEAF, LEAF_PAGE_HEADER_SIZE
from minisqlite.storage.pager import Pager
from minisqlite.storage.record import RecordCodec


class TestBPlusTreeCreation(unittest.TestCase):
    """Test B+Tree creation and initialization."""

    def setUp(self):
        """Create a temporary database file for testing."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

    def tearDown(self):
        """Clean up temporary files."""
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)

    def test_create_btree(self):
        """Test that a B+Tree can be created."""
        pager = Pager(self.db_path)
        # Create header page first
        pager.allocate_page()
        root_page_id = pager.allocate_page()
        btree = BPlusTree(pager, root_page_id)
        self.assertIsInstance(btree, BPlusTree)
        pager.close()

    def test_search_empty_tree(self):
        """Test that searching an empty tree returns None."""
        pager = Pager(self.db_path)
        pager.allocate_page()  # header
        root_page_id = pager.allocate_page()
        btree = BPlusTree(pager, root_page_id)

        result = btree.search(1)
        self.assertIsNone(result)
        pager.close()


class TestBPlusTreeInsert(unittest.TestCase):
    """Test B+Tree insert operations."""

    def setUp(self):
        """Create a temporary database file for testing."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.codec = RecordCodec()

    def tearDown(self):
        """Clean up temporary files."""
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)

    def test_insert_single_row(self):
        """Test inserting a single row."""
        pager = Pager(self.db_path)
        pager.allocate_page()  # header
        root_page_id = pager.allocate_page()
        btree = BPlusTree(pager, root_page_id)

        payload = self.codec.encode([1, "Alice", 30])
        btree.insert(1, payload)

        result = btree.search(1)
        self.assertIsNotNone(result)
        self.assertEqual(result, payload)
        pager.close()

    def test_insert_duplicate_rowid(self):
        """Test that inserting a duplicate rowid raises DuplicateKeyError."""
        pager = Pager(self.db_path)
        pager.allocate_page()  # header
        root_page_id = pager.allocate_page()
        btree = BPlusTree(pager, root_page_id)

        payload1 = self.codec.encode([1, "Alice", 30])
        payload2 = self.codec.encode([1, "Bob", 25])

        btree.insert(1, payload1)

        with self.assertRaises(DuplicateKeyError):
            btree.insert(1, payload2)

        pager.close()

    def test_insert_multiple_rows(self):
        """Test inserting multiple rows."""
        pager = Pager(self.db_path)
        pager.allocate_page()  # header
        root_page_id = pager.allocate_page()
        btree = BPlusTree(pager, root_page_id)

        payloads = []
        for i, (rowid, name, age) in enumerate([(1, "Alice", 30), (2, "Bob", 25), (3, "Carol", 41)]):
            payload = self.codec.encode([rowid, name, age])
            payloads.append((rowid, payload))
            btree.insert(rowid, payload)

        for rowid, expected_payload in payloads:
            result = btree.search(rowid)
            self.assertEqual(result, expected_payload)

        pager.close()

    def test_insert_maintains_order(self):
        """Test that insert maintains rowid order for scan_all."""
        pager = Pager(self.db_path)
        pager.allocate_page()  # header
        root_page_id = pager.allocate_page()
        btree = BPlusTree(pager, root_page_id)

        # Insert in non-sequential order
        btree.insert(3, self.codec.encode([3, "Carol", 41]))
        btree.insert(1, self.codec.encode([1, "Alice", 30]))
        btree.insert(2, self.codec.encode([2, "Bob", 25]))

        results = btree.scan_all()
        rowids = [rowid for rowid, _ in results]

        self.assertEqual(rowids, [1, 2, 3])
        pager.close()


class TestBPlusTreeScan(unittest.TestCase):
    """Test B+Tree scan operations."""

    def setUp(self):
        """Create a temporary database file for testing."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.codec = RecordCodec()

    def tearDown(self):
        """Clean up temporary files."""
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)

    def test_scan_empty_tree(self):
        """Test scanning an empty tree returns empty list."""
        pager = Pager(self.db_path)
        pager.allocate_page()  # header
        root_page_id = pager.allocate_page()
        btree = BPlusTree(pager, root_page_id)

        results = btree.scan_all()
        self.assertEqual(results, [])
        pager.close()

    def test_scan_all_rows(self):
        """Test scanning all rows returns them in rowid order."""
        pager = Pager(self.db_path)
        pager.allocate_page()  # header
        root_page_id = pager.allocate_page()
        btree = BPlusTree(pager, root_page_id)

        # Insert multiple rows
        for rowid, name, age in [(1, "Alice", 30), (2, "Bob", 25), (3, "Carol", 41)]:
            payload = self.codec.encode([rowid, name, age])
            btree.insert(rowid, payload)

        results = btree.scan_all()

        self.assertEqual(len(results), 3)
        self.assertEqual(results[0][0], 1)
        self.assertEqual(results[1][0], 2)
        self.assertEqual(results[2][0], 3)
        pager.close()


class TestBPlusTreeDelete(unittest.TestCase):
    """Test B+Tree delete operations."""

    def setUp(self):
        """Create a temporary database file for testing."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.codec = RecordCodec()

    def tearDown(self):
        """Clean up temporary files."""
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)

    def test_delete_existing_row(self):
        """Test deleting an existing row."""
        pager = Pager(self.db_path)
        pager.allocate_page()  # header
        root_page_id = pager.allocate_page()
        btree = BPlusTree(pager, root_page_id)

        payload = self.codec.encode([1, "Alice", 30])
        btree.insert(1, payload)

        result = btree.delete(1)
        self.assertTrue(result)

        # Verify deletion
        self.assertIsNone(btree.search(1))
        pager.close()

    def test_delete_nonexistent_row(self):
        """Test deleting a nonexistent row returns False."""
        pager = Pager(self.db_path)
        pager.allocate_page()  # header
        root_page_id = pager.allocate_page()
        btree = BPlusTree(pager, root_page_id)

        result = btree.delete(999)
        self.assertFalse(result)
        pager.close()

    def test_delete_then_scan(self):
        """Test that deleted rows do not appear in scan results."""
        pager = Pager(self.db_path)
        pager.allocate_page()  # header
        root_page_id = pager.allocate_page()
        btree = BPlusTree(pager, root_page_id)

        # Insert multiple rows
        for rowid, name, age in [(1, "Alice", 30), (2, "Bob", 25), (3, "Carol", 41)]:
            payload = self.codec.encode([rowid, name, age])
            btree.insert(rowid, payload)

        # Delete middle row
        btree.delete(2)

        results = btree.scan_all()
        rowids = [rowid for rowid, _ in results]

        self.assertEqual(rowids, [1, 3])
        pager.close()


class TestBPlusTreePersistence(unittest.TestCase):
    """Test B+Tree persistence across pager close/reopen."""

    def setUp(self):
        """Create a temporary database file for testing."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.codec = RecordCodec()

    def tearDown(self):
        """Clean up temporary files."""
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)

    def test_persistence_after_close_reopen(self):
        """Test that data persists after closing and reopening the pager."""
        # First session: insert data
        pager1 = Pager(self.db_path)
        pager1.allocate_page()  # header
        root_page_id = pager1.allocate_page()
        btree1 = BPlusTree(pager1, root_page_id)

        payload = self.codec.encode([1, "Alice", 30])
        btree1.insert(1, payload)
        pager1.close()

        # Second session: read data
        pager2 = Pager(self.db_path)
        # Use the same root_page_id from the first session
        # The first allocate_page() returns 1 (header), second returns 2 (B+Tree root)
        root_page_id_2 = root_page_id
        btree2 = BPlusTree(pager2, root_page_id_2)

        result = btree2.search(1)
        self.assertIsNotNone(result)
        self.assertEqual(result, payload)

        results = btree2.scan_all()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], 1)

        pager2.close()


class TestBPlusTreeCapacity(unittest.TestCase):
    """Test B+Tree capacity limits."""

    def setUp(self):
        """Create a temporary database file for testing."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.codec = RecordCodec()

    def tearDown(self):
        """Clean up temporary files."""
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)

    def test_insert_exceeding_page_capacity(self):
        """Test that inserting data exceeding page capacity raises StorageError."""
        pager = Pager(self.db_path)
        pager.allocate_page()  # header
        root_page_id = pager.allocate_page()
        btree = BPlusTree(pager, root_page_id)

        # Create a large payload that exceeds page capacity
        # Page size is 4096, header is 16 bytes, so max payload is ~4080 bytes
        large_text = "X" * 5000
        large_payload = self.codec.encode([1, large_text, 30])

        with self.assertRaises(StorageError):
            btree.insert(1, large_payload)

        pager.close()