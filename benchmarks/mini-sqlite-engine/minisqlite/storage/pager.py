"""Mini SQLite Engine pager.

Provides fixed-size page storage with header read/write and page allocation.
"""

import os
import struct

from minisqlite.errors import CorruptDatabaseError
from minisqlite.storage.file_format import (
    PAGE_SIZE,
    HEADER_SIZE,
    MAGIC,
    FORMAT_VERSION,
    build_header,
    parse_header,
)


class Pager:
    """Manages fixed-size pages in a single binary database file."""

    def __init__(self, path: str):
        self._path = path
        self._file = None
        self._dirty = False
        self._next_page_id = 1
        self._schema_root_page = 0
        self._new_db = False

        if os.path.exists(path):
            self._open_existing()
        else:
            self._new_db = True
            self._open_new()

    def _open_new(self):
        """Create a new database file with a valid header."""
        self._file = open(self._path, "wb+")
        header = build_header(next_page_id=1, schema_root_page=0)
        self._file.write(header)
        # Pad to PAGE_SIZE
        self._file.write(b"\x00" * (PAGE_SIZE - HEADER_SIZE))
        self._file.flush()
        self._next_page_id = 1
        self._schema_root_page = 0

    def _open_existing(self):
        """Open an existing database file and validate the header."""
        self._file = open(self._path, "r+b")
        header_data = self._file.read(HEADER_SIZE)
        if len(header_data) < HEADER_SIZE:
            self._file.close()
            raise CorruptDatabaseError("Database file too small for header")

        parsed = parse_header(header_data)

        if parsed["magic"] != MAGIC:
            self._file.close()
            raise CorruptDatabaseError(
                f"Bad magic bytes: expected {MAGIC!r}, got {parsed['magic']!r}"
            )

        if parsed["page_size"] != PAGE_SIZE:
            self._file.close()
            raise CorruptDatabaseError(
                f"Unsupported page size: {parsed['page_size']}"
            )

        if parsed["format_version"] != FORMAT_VERSION:
            self._file.close()
            raise CorruptDatabaseError(
                f"Unsupported format version: {parsed['format_version']}"
            )

        self._next_page_id = parsed["next_page_id"]
        self._schema_root_page = parsed["schema_root_page"]

    def allocate_page(self) -> int:
        """Allocate the next available page id and increment next_page_id."""
        page_id = self._next_page_id
        self._next_page_id += 1
        self._dirty = True
        return page_id

    def read_page(self, page_id: int) -> bytes:
        """Read exactly PAGE_SIZE bytes for the given page id."""
        offset = page_id * PAGE_SIZE
        self._file.seek(offset)
        data = self._file.read(PAGE_SIZE)
        if len(data) < PAGE_SIZE:
            raise CorruptDatabaseError(
                f"Page {page_id} extends beyond EOF"
            )
        return data

    def write_page(self, page_id: int, data: bytes):
        """Write data to a page. Pads with zeros if shorter than PAGE_SIZE.

        Raises ValueError if data is longer than PAGE_SIZE.
        """
        if len(data) > PAGE_SIZE:
            raise ValueError(
                f"Data too large for page: {len(data)} > {PAGE_SIZE}"
            )

        offset = page_id * PAGE_SIZE
        # Ensure file is large enough
        file_size = os.path.getsize(self._path)
        needed = offset + PAGE_SIZE
        if needed > file_size:
            self._file.seek(needed - 1)
            self._file.write(b"\x00")
            self._file.flush()

        self._file.seek(offset)
        self._file.write(data)
        # Pad remaining bytes if data is shorter than PAGE_SIZE
        if len(data) < PAGE_SIZE:
            padding = b"\x00" * (PAGE_SIZE - len(data))
            self._file.write(padding)
        self._file.flush()
        self._dirty = True

    def _flush_header(self):
        """Write the current header back to page 0."""
        header = build_header(
            next_page_id=self._next_page_id,
            schema_root_page=self._schema_root_page,
        )
        self._file.seek(0)
        self._file.write(header)
        self._file.flush()
        self._dirty = False

    def close(self):
        """Close the database file, flushing header changes."""
        if self._file is not None:
            if self._dirty:
                self._flush_header()
            self._file.close()
            self._file = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
