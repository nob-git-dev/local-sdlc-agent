"""
B+Tree Storage for MiniSQLite

Implements single-page leaf B+Tree operations for the MVP stage.
Supports insert, search, and scan on a single leaf page.
"""

import struct
from typing import List, Optional, Tuple

from minisqlite.errors import CorruptDatabaseError, DuplicateKeyError, StorageError
from minisqlite.storage.pager import Pager
from minisqlite.storage.record import RecordCodec


# Page type constants
PAGE_TYPE_LEAF = 1
PAGE_TYPE_INTERNAL = 2

# Leaf page header size (16 bytes as per spec)
LEAF_PAGE_HEADER_SIZE = 16

# Cell structure:
# - rowid: 8 bytes (signed big-endian)
# - payload_size: 4 bytes (unsigned big-endian)
# - payload: variable


class BPlusTree:
    """
    Single-page B+Tree implementation for MVP.

    This implementation supports:
    - Insert with duplicate key detection
    - Search by rowid
    - Full scan in rowid order
    - Delete by rowid

    It assumes a single leaf page for the MVP stage.
    """

    def __init__(self, pager: Pager, root_page_id: int, is_root: bool = True):
        """
        Initialize a B+Tree.

        Args:
            pager: Pager instance for page I/O
            root_page_id: The page ID of the root page (should be a leaf for MVP)
            is_root: Whether the root page is also the only page (default True for MVP)
        """
        self.pager = pager
        self.is_root = is_root
        self.codec = RecordCodec()
        self._initialized = False

        # Use the caller-provided root_page_id directly.
        # The caller (test or engine) is responsible for passing the correct page ID.
        # This simplifies the B+Tree constructor and avoids hidden logic that can
        # cause test failures when the header's next_page_id doesn't match expectations.
        self.root_page_id = root_page_id

        self._ensure_root_initialized()

    def _ensure_root_initialized(self) -> None:
        """
        Initialize the root leaf page only if it is completely empty (all zeros).
        If the page contains any non-zero bytes, it indicates prior writes and
        must be preserved to maintain persistence across close/reopen cycles.
        """
        if self._initialized:
            return

        page_data = self.pager.read_page(self.root_page_id)

        # Use PAGE_SIZE constant from file_format
        from minisqlite.storage.file_format import PAGE_SIZE

        # Check if page is uninitialized:
        # Only treat as uninitialized if ALL bytes are zero (never written)
        # If ANY byte is non-zero, the page contains data and must be preserved
        is_all_zeros = len(page_data) == PAGE_SIZE and all(b == 0 for b in page_data)

        if is_all_zeros:
            # Initialize as a leaf page with valid header
            page_data = bytearray(PAGE_SIZE)
            page_data[0] = PAGE_TYPE_LEAF  # page_type = 1
            page_data[1] = 1 if self.is_root else 0  # is_root
            # cell_count = 0 (already zero)
            # right_sibling_page_id = 0 (already zero)
            # parent_page_id = 0 (already zero)
            cell_area_start = LEAF_PAGE_HEADER_SIZE  # 16
            page_data[12:14] = struct.pack(">H", cell_area_start)

            self.pager.write_page(self.root_page_id, bytes(page_data))
            self._initialized = True
            return

        # Page contains non-zero bytes - it has been written and must be preserved
        # Do NOT reinitialize - this is the key fix for persistence
        # The page may have page_type=0 if it was allocated but the B+Tree
        # initialization was interrupted, but the cell data is still valid.
        # We must preserve all existing data and only ensure the header is valid.

        # Check if page_type is valid
        page_type = page_data[0] if len(page_data) > 0 else 0

        if page_type == PAGE_TYPE_LEAF:
            # Page is already a valid leaf page, nothing to do
            self._initialized = True
            return

        # For any other page_type (including 0) with non-zero data, skip initialization
        # to preserve existing cell data. The page will be treated as already containing
        # valid cells.
        self._initialized = True

        # CRITICAL FIX: Ensure the page header is valid even if page_type is 0.
        # When a page was allocated but initialization was interrupted, page_type may be 0.
        # We need to fix the header so _read_leaf_page can parse it correctly.
        if page_type == 0:
            # Fix the page_type to LEAF without disturbing cell data
            page_data[0] = PAGE_TYPE_LEAF
            self.pager.write_page(self.root_page_id, bytes(page_data))

    def _get_page_size(self) -> int:
        """Get page size from file_format module."""
        from minisqlite.storage.file_format import PAGE_SIZE
        return PAGE_SIZE

    def _read_leaf_page(self, page_id: int) -> Tuple[int, List[Tuple[int, bytes]]]:
        """
        Read a leaf page and extract cells.

        Args:
            page_id: Page ID to read

        Returns:
            Tuple of (is_root, list of (rowid, payload) tuples)
        """
        page_data = self.pager.read_page(page_id)

        # Parse header
        # Ensure we have enough data before reading
        if len(page_data) < LEAF_PAGE_HEADER_SIZE:
            raise CorruptDatabaseError(
                f"Page {page_id} too small ({len(page_data)} bytes) to contain leaf header"
            )

        page_type = page_data[0]
        is_root_flag = page_data[1]
        cell_count = struct.unpack(">H", page_data[2:4])[0]
        # right_sibling_page_id = struct.unpack(">I", page_data[4:8])[0]
        # parent_page_id = struct.unpack(">I", page_data[8:12])[0]
        cell_area_start = struct.unpack(">H", page_data[12:14])[0]

        # Accept page_type=0 as a valid leaf page if it contains data.
        # This happens when the page was allocated but the header wasn't fully written.
        # We trust the cell_count and cell_area_start fields to read the data.
        if page_type != PAGE_TYPE_LEAF and page_type != 0:
            raise CorruptDatabaseError(
                f"Expected leaf page type {PAGE_TYPE_LEAF}, got {page_type} at page {page_id}"
            )

        # Parse cells
        cells = []
        offset = LEAF_PAGE_HEADER_SIZE
        while offset < cell_area_start and len(cells) < cell_count:
            # Read rowid (8 bytes)
            rowid = struct.unpack(">q", page_data[offset : offset + 8])[0]
            offset += 8

            # Read payload_size (4 bytes)
            payload_size = struct.unpack(">I", page_data[offset : offset + 4])[0]
            offset += 4

            # Read payload
            payload = page_data[offset : offset + payload_size]
            offset += payload_size

            cells.append((rowid, payload))

        # Sort cells by rowid to ensure order (in case they were not inserted in order)
        cells.sort(key=lambda x: x[0])

        return is_root_flag, cells

    def _write_leaf_page(
        self, page_id: int, is_root: bool, cells: List[Tuple[int, bytes]]
    ) -> None:
        """
        Write cells to a leaf page.

        Args:
            page_id: Page ID to write to
            is_root: Whether this is the root page
            cells: List of (rowid, payload) tuples
        """
        # Build cell data
        cell_data = b""
        for rowid, payload in cells:
            cell_data += struct.pack(">q", rowid)  # rowid
            cell_data += struct.pack(">I", len(payload))  # payload_size
            cell_data += payload  # payload

        # Check if it fits in a single page
        required_size = LEAF_PAGE_HEADER_SIZE + len(cell_data)
        from minisqlite.storage.file_format import PAGE_SIZE
        page_size = PAGE_SIZE

        if required_size > page_size:
            raise StorageError(
                f"Cell data ({len(cell_data)} bytes) exceeds page capacity ({page_size - LEAF_PAGE_HEADER_SIZE} bytes)"
            )

        # Build page data
        page_data = bytearray(page_size)
        page_data[0] = PAGE_TYPE_LEAF  # page_type
        page_data[1] = 1 if is_root else 0  # is_root
        page_data[2:4] = struct.pack(">H", len(cells))  # cell_count
        # right_sibling_page_id = 0 (no siblings in MVP)
        # parent_page_id = 0 (no parent in MVP single-page mode)
        cell_area_start = LEAF_PAGE_HEADER_SIZE + len(cell_data)
        page_data[12:14] = struct.pack(">H", cell_area_start)

        # Copy cell data
        page_data[LEAF_PAGE_HEADER_SIZE : LEAF_PAGE_HEADER_SIZE + len(cell_data)] = (
            cell_data
        )

        self.pager.write_page(page_id, bytes(page_data))

    def search(self, rowid: int) -> Optional[bytes]:
        """
        Search for a row by rowid.

        Args:
            rowid: The rowid to search for

        Returns:
            The payload bytes if found, None otherwise
        """
        _, cells = self._read_leaf_page(self.root_page_id)

        # Linear search (could be binary search for sorted cells)
        for r, payload in cells:
            if r == rowid:
                return payload

        return None

    def insert(self, rowid: int, payload: bytes) -> None:
        """
        Insert a new row.

        Args:
            rowid: The rowid for the new row
            payload: The encoded record bytes

        Raises:
            DuplicateKeyError: If the rowid already exists
            StorageError: If the page is full
        """
        is_root, cells = self._read_leaf_page(self.root_page_id)

        # Check for duplicate
        for r, _ in cells:
            if r == rowid:
                raise DuplicateKeyError(f"Duplicate rowid: {rowid}")

        # Insert maintaining rowid order
        insert_pos = 0
        for i, (r, _) in enumerate(cells):
            if r > rowid:
                break
            insert_pos = i + 1

        cells.insert(insert_pos, (rowid, payload))

        # Write back
        self._write_leaf_page(self.root_page_id, is_root, cells)

    def delete(self, rowid: int) -> bool:
        """
        Delete a row by rowid.

        Args:
            rowid: The rowid to delete

        Returns:
            True if deleted, False if not found
        """
        is_root, cells = self._read_leaf_page(self.root_page_id)

        # Find and remove
        new_cells = [(r, p) for r, p in cells if r != rowid]

        if len(new_cells) == len(cells):
            return False  # Not found

        # Write back
        self._write_leaf_page(self.root_page_id, is_root, new_cells)
        return True

    def scan_all(self) -> List[Tuple[int, bytes]]:
        """
        Scan all rows in rowid order.

        Returns:
            List of (rowid, payload) tuples
        """
        _, cells = self._read_leaf_page(self.root_page_id)
        return cells