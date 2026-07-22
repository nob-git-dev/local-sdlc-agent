"""
Pager Tests

Tests for the Pager class to ensure correct page-level I/O operations.
"""

import os
import tempfile
import unittest

from minisqlite.errors import StorageError, CorruptDatabaseError
from minisqlite.storage.pager import Pager
from minisqlite.storage.file_format import (
    MAGIC_BYTES,
    PAGE_SIZE,
    FORMAT_VERSION,
    DatabaseHeader,
)


class TestPagerCreation(unittest.TestCase):
    """Test Pager creation and basic operations."""

    def setUp(self):
        """Create a temporary directory for test databases."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

    def tearDown(self):
        """Clean up temporary files."""
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)

    def test_create_new_database(self):
        """Test creating a new database file."""
        pager = Pager(self.db_path)
        self.assertTrue(os.path.exists(self.db_path))
        self.assertIsNotNone(pager.header)
        pager.close()

    def test_header_magic_bytes(self):
        """Test that the header has correct magic bytes."""
        pager = Pager(self.db_path)
        self.assertEqual(pager.header.magic, MAGIC_BYTES)
        pager.close()

    def test_header_page_size(self):
        """Test that the header has correct page size."""
        pager = Pager(self.db_path)
        self.assertEqual(pager.header.page_size, PAGE_SIZE)
        pager.close()

    def test_header_format_version(self):
        """Test that the header has correct format version."""
        pager = Pager(self.db_path)
        self.assertEqual(pager.header.format_version, FORMAT_VERSION)
        pager.close()

    def test_open_existing_database(self):
        """Test opening an existing database file."""
        # Create a database
        pager1 = Pager(self.db_path)
        pager1.close()

        # Open it again
        pager2 = Pager(self.db_path)
        self.assertEqual(pager2.header.magic, MAGIC_BYTES)
        self.assertEqual(pager2.header.page_size, PAGE_SIZE)
        pager2.close()


class TestPageIO(unittest.TestCase):
    """Test page read/write operations."""

    def setUp(self):
        """Create a temporary directory for test databases."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

    def tearDown(self):
        """Clean up temporary files."""
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)

    def test_write_and_read_page(self):
        """Test writing and reading a page."""
        pager = Pager(self.db_path)

        # Write a page
        page_data = b"A" * PAGE_SIZE
        pager.write_page(1, page_data)

        # Read it back
        read_data = pager.read_page(1)
        self.assertEqual(read_data, page_data)

        pager.close()

    def test_read_nonexistent_page(self):
        """Test reading a page that hasn't been written yet."""
        pager = Pager(self.db_path)

        # Read an unwritten page (should return zeros)
        # Note: This will fail because we haven't allocated the page
        # This test documents expected behavior
        with self.assertRaises(StorageError):
            pager.read_page(1)

        pager.close()

    def test_invalid_page_id_negative(self):
        """Test that negative page IDs raise an error."""
        pager = Pager(self.db_path)

        with self.assertRaises(StorageError):
            pager.read_page(-1)

        with self.assertRaises(StorageError):
            pager.write_page(-1, b"\x00" * PAGE_SIZE)

        pager.close()

    def test_invalid_page_data_size(self):
        """Test that writing data of wrong size raises an error."""
        pager = Pager(self.db_path)

        # Too small
        with self.assertRaises(StorageError):
            pager.write_page(1, b"small")

        # Too large
        with self.assertRaises(StorageError):
            pager.write_page(1, b"\x00" * (PAGE_SIZE + 1))

        pager.close()

    def test_allocate_page(self):
        """Test allocating a new page."""
        pager = Pager(self.db_path)

        initial_next_id = pager.next_page_id
        page_id = pager.allocate_page()

        self.assertEqual(page_id, initial_next_id)
        self.assertEqual(pager.next_page_id, initial_next_id + 1)

        # Verify the page is initialized to zeros
        page_data = pager.read_page(page_id)
        self.assertEqual(page_data, b"\x00" * PAGE_SIZE)

        pager.close()

    def test_multiple_page_allocations(self):
        """Test allocating multiple pages."""
        pager = Pager(self.db_path)

        page_ids = []
        for i in range(5):
            page_id = pager.allocate_page()
            page_ids.append(page_id)

        # Verify sequential allocation
        self.assertEqual(page_ids, [1, 2, 3, 4, 5])

        pager.close()


class TestCorruptionDetection(unittest.TestCase):
    """Test corruption detection in database files."""

    def setUp(self):
        """Create a temporary directory for test databases."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

    def tearDown(self):
        """Clean up temporary files."""
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)

    def test_invalid_magic_bytes(self):
        """Test that invalid magic bytes raise CorruptDatabaseError."""
        # Create a file with invalid magic bytes
        with open(self.db_path, "wb") as f:
            f.write(b"INVALID!" + b"\x00" * (PAGE_SIZE - 8))

        with self.assertRaises(CorruptDatabaseError):
            Pager(self.db_path)

    def test_invalid_page_size(self):
        """Test that invalid page size raises CorruptDatabaseError."""
        # Create a file with invalid page size
        header = bytearray(PAGE_SIZE)
        header[0:8] = MAGIC_BYTES
        header[8:12] = (1024).to_bytes(4, byteorder="big")  # Wrong page size

        with open(self.db_path, "wb") as f:
            f.write(bytes(header))

        with self.assertRaises(CorruptDatabaseError):
            Pager(self.db_path)

    def test_invalid_format_version(self):
        """Test that unsupported format version raises CorruptDatabaseError."""
        # Create a file with invalid format version
        header = bytearray(PAGE_SIZE)
        header[0:8] = MAGIC_BYTES
        header[8:12] = PAGE_SIZE.to_bytes(4, byteorder="big")
        header[12:16] = (999).to_bytes(4, byteorder="big")  # Wrong version

        with open(self.db_path, "wb") as f:
            f.write(bytes(header))

        with self.assertRaises(CorruptDatabaseError):
            Pager(self.db_path)

    def test_file_too_small(self):
        """Test that a file smaller than header size raises CorruptDatabaseError."""
        # Create a file that's too small
        with open(self.db_path, "wb") as f:
            f.write(b"MSQLITE1")  # Only magic bytes, no full header

        with self.assertRaises(CorruptDatabaseError):
            Pager(self.db_path)


class TestPersistence(unittest.TestCase):
    """Test persistence across close/reopen cycles."""

    def setUp(self):
        """Create a temporary directory for test databases."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

    def tearDown(self):
        """Clean up temporary files."""
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)

    def test_persist_and_restore_page(self):
        """Test that written pages persist after close/reopen."""
        # Create and write
        pager1 = Pager(self.db_path)
        page_data = b"X" * PAGE_SIZE
        pager1.write_page(1, page_data)
        pager1.flush()
        pager1.close()

        # Reopen and read
        pager2 = Pager(self.db_path)
        restored_data = pager2.read_page(1)
        self.assertEqual(restored_data, page_data)
        pager2.close()

    def test_persist_multiple_pages(self):
        """Test that multiple pages persist after close/reopen."""
        pager1 = Pager(self.db_path)

        # Write multiple pages
        for i in range(1, 6):
            page_data = bytes([i] * PAGE_SIZE)
            pager1.write_page(i, page_data)

        pager1.flush()
        pager1.close()

        # Reopen and verify
        pager2 = Pager(self.db_path)
        for i in range(1, 6):
            expected = bytes([i] * PAGE_SIZE)
            actual = pager2.read_page(i)
            self.assertEqual(actual, expected, f"Page {i} mismatch")

        pager2.close()

    def test_persist_allocated_pages(self):
        """Test that allocated pages persist after close/reopen."""
        pager1 = Pager(self.db_path)

        # Allocate pages
        page_ids = [pager1.allocate_page() for _ in range(3)]

        # Write data to allocated pages
        for page_id in page_ids:
            page_data = b"Y" * PAGE_SIZE
            pager1.write_page(page_id, page_data)

        pager1.flush()
        pager1.close()

        # Reopen and verify
        pager2 = Pager(self.db_path)
        for page_id in page_ids:
            actual = pager2.read_page(page_id)
            self.assertEqual(actual, b"Y" * PAGE_SIZE)

        pager2.close()


class TestContextManager(unittest.TestCase):
    """Test Pager as a context manager."""

    def setUp(self):
        """Create a temporary directory for test databases."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

    def tearDown(self):
        """Clean up temporary files."""
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)

    def test_context_manager(self):
        """Test using Pager as a context manager."""
        with Pager(self.db_path) as pager:
            pager.write_page(1, b"Z" * PAGE_SIZE)

        # File should be closed and flushed
        self.assertTrue(os.path.exists(self.db_path))

        # Verify data persisted
        with Pager(self.db_path) as pager2:
            data = pager2.read_page(1)
            self.assertEqual(data, b"Z" * PAGE_SIZE)