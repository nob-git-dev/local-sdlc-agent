import os
import struct
import tempfile
import unittest

from minisqlite.errors import CorruptDatabaseError, StorageError
from minisqlite.storage.file_format import (
    FORMAT_VERSION,
    MAGIC,
    PAGE_SIZE,
    create_page0,
    parse_header,
)
from minisqlite.storage.pager import Pager


class PagerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "test.db")

    def _page0(self):
        with open(self.path, "rb") as f:
            return f.read(PAGE_SIZE)

    def test_new_db_page0_magic_page_size_version(self):
        pager = Pager(self.path)
        pager.close()
        page0 = self._page0()
        parsed = parse_header(page0)
        self.assertEqual(parsed["magic"], MAGIC)
        self.assertEqual(parsed["page_size"], PAGE_SIZE)
        self.assertEqual(parsed["format_version"], FORMAT_VERSION)

    def test_close_reopen(self):
        pager = Pager(self.path)
        pager.close()
        pager2 = Pager(self.path)
        self.assertEqual(pager2.read_page(0), self._page0())
        pager2.close()

    def test_allocate_1_2_3(self):
        pager = Pager(self.path)
        self.assertEqual(pager.allocate_page(), 1)
        self.assertEqual(pager.allocate_page(), 2)
        self.assertEqual(pager.allocate_page(), 3)
        pager.close()

        pager2 = Pager(self.path)
        self.assertEqual(pager2.read_page(1), b"\x00" * PAGE_SIZE)
        self.assertEqual(pager2.read_page(2), b"\x00" * PAGE_SIZE)
        self.assertEqual(pager2.read_page(3), b"\x00" * PAGE_SIZE)
        pager2.close()

    def test_page_read_write_and_persistence(self):
        pager = Pager(self.path)
        page_id = pager.allocate_page()
        data = bytearray(PAGE_SIZE)
        data[0:7] = b"payload"
        pager.write_page(page_id, bytes(data))
        self.assertEqual(pager.read_page(page_id), bytes(data))
        pager.close()

        pager2 = Pager(self.path)
        self.assertEqual(pager2.read_page(page_id), bytes(data))
        pager2.close()

    def test_write_wrong_size_raises(self):
        pager = Pager(self.path)
        page_id = pager.allocate_page()
        with self.assertRaises(ValueError):
            pager.write_page(page_id, b"short")
        pager.close()

    def test_read_nonexistent_page_raises(self):
        pager = Pager(self.path)
        with self.assertRaises(StorageError):
            pager.read_page(1)
        pager.close()

    def test_bad_magic_raises(self):
        with open(self.path, "wb") as f:
            f.write(b"BADMAGIC" + b"\x00" * (PAGE_SIZE - 8))
        with self.assertRaises(CorruptDatabaseError):
            Pager(self.path)

    def test_wrong_page_size_raises(self):
        header = struct.pack(
            ">8s4I8s",
            MAGIC,
            512,
            FORMAT_VERSION,
            1,
            0,
            b"\x00" * 8,
        )
        with open(self.path, "wb") as f:
            f.write(header + b"\x00" * (PAGE_SIZE - 32))
        with self.assertRaises(CorruptDatabaseError):
            Pager(self.path)

    def test_unsupported_version_raises(self):
        header = struct.pack(
            ">8s4I8s",
            MAGIC,
            PAGE_SIZE,
            999,
            1,
            0,
            b"\x00" * 8,
        )
        with open(self.path, "wb") as f:
            f.write(header + b"\x00" * (PAGE_SIZE - 32))
        with self.assertRaises(CorruptDatabaseError):
            Pager(self.path)

    def test_short_file_raises(self):
        with open(self.path, "wb") as f:
            f.write(b"\x00" * 100)
        with self.assertRaises(CorruptDatabaseError):
            Pager(self.path)

    def test_non_multiple_of_page_size_raises(self):
        with open(self.path, "wb") as f:
            f.write(create_page0(1, 0))
            f.write(b"\x00" * 100)
        with self.assertRaises(CorruptDatabaseError):
            Pager(self.path)

    def test_operation_after_close_raises(self):
        pager = Pager(self.path)
        pager.close()
        with self.assertRaises(AttributeError):
            pager.read_page(0)


if __name__ == "__main__":
    unittest.main()