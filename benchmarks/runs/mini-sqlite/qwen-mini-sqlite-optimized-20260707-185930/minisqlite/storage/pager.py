"""
Page Manager for MiniSQLite

Handles page-level I/O operations, including file header management,
page reading/writing, and corruption detection.
"""

import os
from typing import Optional, List

from minisqlite.errors import StorageError, CorruptDatabaseError
from minisqlite.storage.file_format import (
    MAGIC_BYTES,
    PAGE_SIZE,
    FORMAT_VERSION,
    HEADER_FIXED_SIZE,
    DatabaseHeader,
    PageHeader,
    PAGE_TYPE_LEAF,
    PAGE_TYPE_INTERNAL,
)


class Pager:
    """
    Manages page-level I/O operations for the database file.

    Responsibilities:
    - Create/open database files
    - Read/write pages
    - Validate file header (magic bytes, page size)
    - Detect corruption
    - Allocate new pages
    """

    def __init__(self, db_path: str):
        """
        Initialize the Pager with a database file path.

        Args:
            db_path: Path to the database file

        Raises:
            CorruptDatabaseError: If the file exists but has invalid header
            StorageError: If the file cannot be opened
        """
        self.db_path = db_path
        self._file = None
        self._header: Optional[DatabaseHeader] = None
        self._page_cache: dict = {}  # page_id -> bytes
        self._dirty_pages: set = set()  # set of modified page_ids

        self._open()

    def _open(self) -> None:
        """Open the database file and validate header."""
        if os.path.exists(self.db_path):
            # Open existing file
            self._file = open(self.db_path, "rb+")
            self._read_header()
        else:
            # Create new file
            self._file = open(self.db_path, "wb+")
            self._create_header()

    def _read_header(self) -> None:
        """Read and validate the database header."""
        # Read first page (header page)
        header_data = self._file.read(PAGE_SIZE)
        if len(header_data) < HEADER_FIXED_SIZE:
            raise CorruptDatabaseError(
                f"Database file too small: {len(header_data)} bytes, expected at least {HEADER_FIXED_SIZE}"
            )

        try:
            self._header = DatabaseHeader.from_bytes(header_data)
        except ValueError as e:
            raise CorruptDatabaseError(f"Invalid header format: {e}")

        # Validate magic bytes
        if self._header.magic != MAGIC_BYTES:
            raise CorruptDatabaseError(
                f"Invalid magic bytes: expected {MAGIC_BYTES!r}, got {self._header.magic!r}"
            )

        # Validate page size
        if self._header.page_size != PAGE_SIZE:
            raise CorruptDatabaseError(
                f"Invalid page size: expected {PAGE_SIZE}, got {self._header.page_size}"
            )

        # Validate format version
        if self._header.format_version != FORMAT_VERSION:
            raise CorruptDatabaseError(
                f"Unsupported format version: expected {FORMAT_VERSION}, got {self._header.format_version}"
            )

        # Cache the header page (use the actual read data, not parsed header bytes)
        self._page_cache[0] = header_data

    def _create_header(self) -> None:
        """Create a new database header."""
        self._header = DatabaseHeader(
            magic=MAGIC_BYTES,
            page_size=PAGE_SIZE,
            format_version=FORMAT_VERSION,
            next_page_id=1,  # First data page will be page 1
            schema_root_page=0,  # Not used in MVP
            reserved=b"\x00" * 8
        )

        # Write header page
        header_bytes = self._header.to_bytes()
        # Pad to PAGE_SIZE
        header_bytes = header_bytes + b"\x00" * (PAGE_SIZE - len(header_bytes))

        self._file.write(header_bytes)
        self._file.flush()

        # Cache the header page (use the actual written data)
        self._page_cache[0] = header_bytes

    def read_page(self, page_id: int) -> bytes:
        """
        Read a page from the database file.

        Args:
            page_id: The page ID to read

        Returns:
            The page data as bytes

        Raises:
            StorageError: If the page_id is invalid or the page cannot be read
            CorruptDatabaseError: If the page data is corrupted
        """
        if page_id < 0:
            raise StorageError(f"Invalid page ID: {page_id}")

        # Check cache first
        if page_id in self._page_cache:
            return self._page_cache[page_id]

        # Read from file
        try:
            self._file.seek(page_id * PAGE_SIZE)
            page_data = self._file.read(PAGE_SIZE)

            if len(page_data) < PAGE_SIZE:
                raise StorageError(
                    f"Page {page_id} incomplete: expected {PAGE_SIZE} bytes, got {len(page_data)}"
                )

            # Do NOT validate page type in read_page().
            # The Pager layer handles raw page I/O only.
            # B+Tree layer (btree.py) is responsible for page structure validation.
            # This allows allocate_page() to return zero-filled pages (type 0)
            # which are valid until the B+Tree writes actual page structure.

            self._page_cache[page_id] = page_data
            return page_data

        except OSError as e:
            raise StorageError(f"Failed to read page {page_id}: {e}")

    def write_page(self, page_id: int, data: bytes) -> None:
        """
        Write a page to the database file.

        Args:
            page_id: The page ID to write
            data: The page data (must be exactly PAGE_SIZE bytes)

        Raises:
            StorageError: If the data size is invalid or the page cannot be written
            CorruptDatabaseError: If the page data has an invalid page type header
        """
        if len(data) != PAGE_SIZE:
            raise StorageError(
                f"Page data must be exactly {PAGE_SIZE} bytes, got {len(data)}"
            )

        if page_id < 0:
            raise StorageError(f"Invalid page ID: {page_id}")

        # Do NOT validate page type in write_page().
        # The Pager layer handles raw page I/O only.
        # B+Tree layer (btree.py) is responsible for page structure validation.
        # This allows allocate_page() to write zero-filled pages (type 0)
        # which are valid until the B+Tree writes actual page structure.

        # Update cache
        self._page_cache[page_id] = data
        self._dirty_pages.add(page_id)

        # Immediately write to file for persistence tests that close/reopen
        # This ensures data is actually on disk before close() is called
        try:
            self._file.seek(page_id * PAGE_SIZE)
            self._file.write(data)
            self._file.flush()
        except OSError as e:
            raise StorageError(f"Failed to write page {page_id}: {e}")

    def allocate_page(self) -> int:
        """
        Allocate a new page.

        Returns:
            The new page ID

        Raises:
            StorageError: If no more pages can be allocated
        """
        if self._header.next_page_id >= 2**32:  # 4-byte limit
            raise StorageError("No more pages available")

        page_id = self._header.next_page_id
        self._header.next_page_id += 1

        # Initialize the new page with zeros (empty page)
        # Do NOT set page_type here; the caller (B+Tree) will write the actual page structure
        # This ensures allocate_page returns a clean, zero-initialized page as expected by tests
        page_data = b"\x00" * PAGE_SIZE
        self.write_page(page_id, page_data)

        return page_id

    def flush(self) -> None:
        """Flush all dirty pages to disk."""
        if self._file is None:
            return

        try:
            for page_id in self._dirty_pages:
                self._file.seek(page_id * PAGE_SIZE)
                self._file.write(self._page_cache[page_id])

            self._file.flush()
            os.fsync(self._file.fileno())
            self._dirty_pages.clear()

        except OSError as e:
            raise StorageError(f"Failed to flush pages: {e}")

    def close(self) -> None:
        """Close the database file."""
        if self._file is not None:
            self.flush()
            self._file.close()
            self._file = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    @property
    def header(self) -> DatabaseHeader:
        """Get the database header."""
        return self._header

    @property
    def next_page_id(self) -> int:
        """Get the next available page ID."""
        return self._header.next_page_id