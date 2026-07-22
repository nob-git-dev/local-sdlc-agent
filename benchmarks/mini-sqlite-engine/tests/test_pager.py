"""Tests for the Mini SQLite Engine pager."""

import os
import tempfile
import unittest

from minisqlite.errors import CorruptDatabaseError
from minisqlite.storage.file_format import PAGE_SIZE, HEADER_SIZE, MAGIC
from minisqlite.storage.pager import Pager


class TestPagerNewDB(unittest.TestCase):
    """Test creating and opening a new database file."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db_path = os.path.join(self._tmpdir, "test.db")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_new_db_file_is_created_with_valid_header(self):
        """A new DB file is created with a valid header."""
        pager = Pager(self._db_path)
        try:
            # Read page 0 (header page)
            header_page = pager.read_page(0)
            self.assertEqual(len(header_page), PAGE_SIZE)
            # Check magic bytes
            self.assertEqual(header_page[:8], MAGIC)
            # Check page size field (offset 8, 4 bytes big-endian)
            page_size = int.from_bytes(header_page[8:12], "big")
            self.assertEqual(page_size, PAGE_SIZE)
            # Check format version (offset 12, 4 bytes big-endian)
            format_version = int.from_bytes(header_page[12:16], "big")
            self.assertEqual(format_version, 1)
        finally:
            pager.close()

    def test_header_can_be_read_after_close_reopen(self):
        """Header can be read after close/reopen."""
        # Create and close
        pager1 = Pager(self._db_path)
        pager1.close()

        # Reopen and read header
        pager2 = Pager(self._db_path)
        try:
            header_page = pager2.read_page(0)
            self.assertEqual(header_page[:8], MAGIC)
            page_size = int.from_bytes(header_page[8:12], "big")
            self.assertEqual(page_size, PAGE_SIZE)
            format_version = int.from_bytes(header_page[12:16], "big")
            self.assertEqual(format_version, 1)
        finally:
            pager2.close()


class TestPagerPageReadWrite(unittest.TestCase):
    """Test page read/write operations."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db_path = os.path.join(self._tmpdir, "test.db")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_page_can_be_written_and_read_back(self):
        """Page can be written and read back."""
        pager = Pager(self._db_path)
        try:
            # Allocate a page
            page_id = pager.allocate_page()
            self.assertEqual(page_id, 1)

            # Write data to the page
            data = b"Hello, World!" + b"\x00" * (PAGE_SIZE - 13)
            pager.write_page(page_id, data)

            # Read it back
            read_back = pager.read_page(page_id)
            self.assertEqual(read_back, data)
        finally:
            pager.close()

    def test_allocate_page_increments_page_ids(self):
        """allocate_page returns the next page id and increments header next_page_id."""
        pager = Pager(self._db_path)
        try:
            page_id_1 = pager.allocate_page()
            page_id_2 = pager.allocate_page()
            page_id_3 = pager.allocate_page()

            self.assertEqual(page_id_1, 1)
            self.assertEqual(page_id_2, 2)
            self.assertEqual(page_id_3, 3)
        finally:
            pager.close()

    def test_oversized_page_write_errors(self):
        """Oversized page write raises ValueError."""
        pager = Pager(self._db_path)
        try:
            page_id = pager.allocate_page()
            with self.assertRaises(ValueError):
                pager.write_page(page_id, b"\x00" * (PAGE_SIZE + 1))
        finally:
            pager.close()

    def test_reading_missing_page_errors(self):
        """Reading a page beyond EOF raises CorruptDatabaseError."""
        pager = Pager(self._db_path)
        try:
            with self.assertRaises(CorruptDatabaseError):
                pager.read_page(999)
        finally:
            pager.close()


class TestPagerBadMagic(unittest.TestCase):
    """Test corruption detection on existing files."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db_path = os.path.join(self._tmpdir, "bad_magic.db")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_bad_magic_bytes_are_detected(self):
        """Bad magic bytes raise CorruptDatabaseError."""
        # Create a file with bad magic
        with open(self._db_path, "wb") as f:
            f.write(b"BADMAGIC" + b"\x00" * (PAGE_SIZE - 8))

        with self.assertRaises(CorruptDatabaseError):
            Pager(self._db_path)

    def test_wrong_page_size_is_detected(self):
        """Wrong page size raises CorruptDatabaseError."""
        import struct

        with open(self._db_path, "wb") as f:
            # Write valid magic, wrong page size, valid version
            header = struct.Struct(">8sIIIIq").pack(
                MAGIC, 8192,  # wrong page size
                1,  # format version
                1, 0, 0
            )
            f.write(header + b"\x00" * (PAGE_SIZE - HEADER_SIZE))

        with self.assertRaises(CorruptDatabaseError):
            Pager(self._db_path)

    def test_unsupported_format_version_is_detected(self):
        """Unsupported format version raises CorruptDatabaseError."""
        import struct

        with open(self._db_path, "wb") as f:
            header = struct.Struct(">8sIIIIq").pack(
                MAGIC,
                PAGE_SIZE,
                99,  # unsupported version
                1, 0, 0
            )
            f.write(header + b"\x00" * (PAGE_SIZE - HEADER_SIZE))

        with self.assertRaises(CorruptDatabaseError):
            Pager(self._db_path)


if __name__ == "__main__":
    unittest.main()
