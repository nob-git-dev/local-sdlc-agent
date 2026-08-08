"""Pager for MiniSQLite Engine.

This module implements the pager responsibilities described in SPEC.md
section 14.7: DB file creation, header read/write, page read/write,
new page allocation, and close-time flush.  Page IDs are 0-based;
page 0 is the header-only page and pages 1+ are B+Tree pages.
"""

import os

from minisqlite.errors import CorruptDatabaseError, StorageError
from minisqlite.storage.file_format import (
    FORMAT_VERSION,
    HEADER_SIZE,
    MAGIC,
    PAGE_SIZE,
    create_page0,
    parse_header,
    validate_page0,
)


class Pager:
    """Manage the database file as a sequence of fixed-length pages."""

    def __init__(self, path):
        self._path = path
        self._file = None
        self._next_page_id = 1
        self._dirty = False
        self._open()

    def _open(self):
        """Open the database file, creating or validating it as needed."""
        exists = os.path.exists(self._path)
        if exists:
            self._file = open(self._path, "r+b")
            try:
                self._validate_existing()
            except Exception:
                self._file.close()
                self._file = None
                raise
        else:
            self._file = open(self._path, "w+b")
            self._write_page0(create_page0(1, 0))
            self._next_page_id = 1
            self._dirty = False

    def _validate_existing(self):
        """Validate magic bytes, page size, version, and file length."""
        self._file.seek(0)
        page0 = self._file.read(PAGE_SIZE)
        if len(page0) != PAGE_SIZE:
            raise CorruptDatabaseError(
                "page 0 must be exactly %d bytes, got %d" % (PAGE_SIZE, len(page0))
            )
        parsed = parse_header(page0)
        if parsed["magic"] != MAGIC:
            raise CorruptDatabaseError("invalid magic bytes in database header")
        if parsed["page_size"] != PAGE_SIZE:
            raise CorruptDatabaseError(
                "page_size mismatch: expected %d, got %d"
                % (PAGE_SIZE, parsed["page_size"])
            )
        if parsed["format_version"] != FORMAT_VERSION:
            raise CorruptDatabaseError(
                "format_version mismatch: expected %d, got %d"
                % (FORMAT_VERSION, parsed["format_version"])
            )
        validate_page0(page0)
        self._next_page_id = parsed["next_page_id"]
        file_length = os.path.getsize(self._path)
        if file_length % PAGE_SIZE != 0:
            raise CorruptDatabaseError(
                "file length %d is not a multiple of page size %d"
                % (file_length, PAGE_SIZE)
            )
        if file_length < PAGE_SIZE:
            raise CorruptDatabaseError(
                "file length %d is smaller than page size %d"
                % (file_length, PAGE_SIZE)
            )

    def _write_page0(self, page0):
        """Write a full page 0 to the file."""
        self._file.seek(0)
        self._file.write(page0)
        self._file.flush()

    def read_page(self, page_id):
        """Read and return the page with the given 0-based page id."""
        if page_id < 0:
            raise ValueError("page_id must be non-negative")
        if page_id >= self._next_page_id:
            raise CorruptDatabaseError(
                "page_id %d out of range: next_page_id is %d"
                % (page_id, self._next_page_id)
            )
        self._file.seek(page_id * PAGE_SIZE)
        data = self._file.read(PAGE_SIZE)
        if len(data) != PAGE_SIZE:
            raise CorruptDatabaseError(
                "page %d is short: expected %d bytes, got %d"
                % (page_id, PAGE_SIZE, len(data))
            )
        return data

    def write_page(self, page_id, data):
        """Write exactly PAGE_SIZE bytes to the given page id."""
        if page_id < 0:
            raise ValueError("page_id must be non-negative")
        if len(data) != PAGE_SIZE:
            raise ValueError(
                "page data must be exactly %d bytes, got %d"
                % (PAGE_SIZE, len(data))
            )
        if page_id >= self._next_page_id:
            raise StorageError(
                "page_id %d out of range: next_page_id is %d"
                % (page_id, self._next_page_id)
            )
        self._file.seek(page_id * PAGE_SIZE)
        self._file.write(data)
        self._dirty = True

    def allocate_page(self):
        """Allocate a new page and return its 0-based page id."""
        page_id = self._next_page_id
        self._next_page_id += 1
        self._file.seek(page_id * PAGE_SIZE)
        self._file.write(b"\x00" * PAGE_SIZE)
        self._dirty = True
        return page_id

    def flush(self):
        """Persist the header and any pending writes to disk."""
        if self._dirty:
            header = create_page0(self._next_page_id, 0)
            self._file.seek(0)
            self._file.write(header)
            self._file.flush()
            os.fsync(self._file.fileno())
            self._dirty = False

    def close(self):
        """Flush pending writes and close the underlying file."""
        if self._file is not None:
            self.flush()
            self._file.close()
            self._file = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False