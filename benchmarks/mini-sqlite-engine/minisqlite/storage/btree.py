"""Mini SQLite Engine multi-leaf B+Tree.

Supports single-leaf and multi-leaf B+Tree with internal root page.
"""

import struct
from minisqlite.errors import (
    DuplicateKeyError,
    StorageError,
    CorruptDatabaseError,
)
from minisqlite.storage.file_format import PAGE_SIZE

# Leaf page format:
#   [0:4]   magic: b"BTLF" (4 bytes)
#   [4:8]   version: uint32 (1)
#   [8:12]  num_cells: uint32
#   [12:16] cell_offset_start: uint32 (start of cell area)
#   [16:PAGE_SIZE-4] cell area (variable)
#   [PAGE_SIZE-4:PAGE_SIZE] overflow_flag: uint32 (1 if overflow, 0 otherwise)

LEAF_MAGIC = b"BTLF"
LEAF_VERSION = 1
LEAF_HEADER_FORMAT = struct.Struct(">4sIII")
LEAF_HEADER_SIZE = LEAF_HEADER_FORMAT.size  # 16
LEAF_OVERFLOW_OFFSET = PAGE_SIZE - 4
LEAF_OVERFLOW_FORMAT = struct.Struct(">I")

# Internal root page format:
#   [0:4]   magic: b"BTIR" (4 bytes)
#   [4:8]   version: uint32 (1)
#   [8:12]  num_children: uint32
#   [12:16] reserved (unused, for alignment)
#   [16:PAGE_SIZE-4] repeated (max_key: uint64, child_page_id: uint32)
#   [PAGE_SIZE-4:PAGE_SIZE] reserved

INTERNAL_MAGIC = b"BTIR"
INTERNAL_VERSION = 1
INTERNAL_HEADER_FORMAT = struct.Struct(">4sIII")
INTERNAL_HEADER_SIZE = INTERNAL_HEADER_FORMAT.size  # 16
INTERNAL_ENTRY_FORMAT = struct.Struct(">QI")
INTERNAL_ENTRY_SIZE = INTERNAL_ENTRY_FORMAT.size  # 12
INTERNAL_RESERVED_OFFSET = PAGE_SIZE - 4


def _write_leaf_overflow(page_data: bytearray, overflow: bool) -> None:
    flag = 1 if overflow else 0
    page_data[LEAF_OVERFLOW_OFFSET:PAGE_SIZE] = LEAF_OVERFLOW_FORMAT.pack(flag)


def _read_leaf_overflow(page_data: bytes) -> bool:
    flag = LEAF_OVERFLOW_FORMAT.unpack(
        page_data[LEAF_OVERFLOW_OFFSET:PAGE_SIZE]
    )[0]
    return flag == 1


def _encode_cell(rowid: int, payload: bytes) -> bytes:
    """Encode a single (rowid, payload) cell.

    Layout: [rowid: uint64][payload_len: uint32][payload: bytes]
    """
    return struct.pack(">QI", rowid, len(payload)) + payload


def _decode_cell(data: bytes, offset: int):
    """Decode a single cell at the given offset.

    Returns (rowid, payload, next_offset).
    """
    rowid, payload_len = struct.unpack_from(">QI", data, offset)
    payload_start = offset + 12
    payload = data[payload_start:payload_start + payload_len]
    next_offset = payload_start + payload_len
    return rowid, payload, next_offset


def _cells_fit_in_page(cells: list[tuple[int, bytes]]) -> bool:
    """Check if all cells fit in a single leaf page."""
    cell_data = bytearray()
    for rowid, payload in cells:
        cell_data.extend(_encode_cell(rowid, payload))
    return LEAF_HEADER_SIZE + len(cell_data) <= (PAGE_SIZE - 4)


def _build_leaf_page(cells: list[tuple[int, bytes]]) -> bytes:
    """Build a leaf page from cells. Returns bytes of PAGE_SIZE."""
    cell_data = bytearray()
    for rowid, payload in cells:
        cell_data.extend(_encode_cell(rowid, payload))

    num_cells = len(cells)
    cell_offset = LEAF_HEADER_SIZE
    overflow = cell_offset + len(cell_data) > (PAGE_SIZE - 4)

    header = LEAF_HEADER_FORMAT.pack(
        LEAF_MAGIC, LEAF_VERSION, num_cells, cell_offset
    )
    page = bytearray(PAGE_SIZE)
    page[0:LEAF_HEADER_SIZE] = header
    page[LEAF_HEADER_SIZE:LEAF_HEADER_SIZE + len(cell_data)] = cell_data
    _write_leaf_overflow(page, overflow)
    return bytes(page)


def _load_cells_from_page(raw: bytes) -> list[tuple[int, bytes]]:
    """Load cells from a leaf page. Raises CorruptDatabaseError on bad data."""
    if len(raw) < LEAF_HEADER_SIZE:
        raise CorruptDatabaseError("BTree leaf page too short for header")
    magic, version, num_cells, cell_offset = LEAF_HEADER_FORMAT.unpack(
        raw[0:LEAF_HEADER_SIZE]
    )
    if magic != LEAF_MAGIC:
        raise CorruptDatabaseError(
            f"Invalid BTree leaf magic: {magic!r}"
        )
    if version != LEAF_VERSION:
        raise CorruptDatabaseError(
            f"Unsupported BTree leaf version: {version}"
        )
    cells = []
    offset = cell_offset
    for _ in range(num_cells):
        if offset + 12 > PAGE_SIZE:
            raise CorruptDatabaseError(
                "Cell offset beyond page boundary"
            )
        rowid, payload_len = struct.unpack_from(">QI", raw, offset)
        payload_start = offset + 12
        if payload_start + payload_len > PAGE_SIZE:
            raise CorruptDatabaseError(
                "Cell payload extends beyond page boundary"
            )
        payload = raw[payload_start:payload_start + payload_len]
        offset = payload_start + payload_len
        cells.append((rowid, payload))
    cells.sort(key=lambda c: c[0])
    return cells


def _build_internal_root_page(children: list[tuple[int, int]]) -> bytes:
    """Build an internal root page from (max_key, child_page_id) entries.

    children must be sorted by max_key ascending.
    """
    num_children = len(children)
    header = INTERNAL_HEADER_FORMAT.pack(
        INTERNAL_MAGIC, INTERNAL_VERSION, num_children, 0
    )
    page = bytearray(PAGE_SIZE)
    page[0:INTERNAL_HEADER_SIZE] = header
    for i, (max_key, child_page_id) in enumerate(children):
        start = INTERNAL_HEADER_SIZE + i * INTERNAL_ENTRY_SIZE
        end = start + INTERNAL_ENTRY_SIZE
        page[start:end] = INTERNAL_ENTRY_FORMAT.pack(max_key, child_page_id)
    return bytes(page)


def _load_internal_root(raw: bytes) -> list[tuple[int, int]]:
    """Load children from an internal root page.

    Returns list of (max_key, child_page_id) sorted by max_key.
    """
    if len(raw) < INTERNAL_HEADER_SIZE:
        raise CorruptDatabaseError(
            "BTree internal root page too short for header"
        )
    magic, version, num_children, _reserved = INTERNAL_HEADER_FORMAT.unpack(
        raw[0:INTERNAL_HEADER_SIZE]
    )
    if magic != INTERNAL_MAGIC:
        raise CorruptDatabaseError(
            f"Invalid BTree internal root magic: {magic!r}"
        )
    if version != INTERNAL_VERSION:
        raise CorruptDatabaseError(
            f"Unsupported BTree internal root version: {version}"
        )
    children = []
    for i in range(num_children):
        start = INTERNAL_HEADER_SIZE + i * INTERNAL_ENTRY_SIZE
        end = start + INTERNAL_ENTRY_SIZE
        max_key, child_page_id = INTERNAL_ENTRY_FORMAT.unpack(
            raw[start:end]
        )
        children.append((max_key, child_page_id))
    children.sort(key=lambda c: c[0])
    return children


class BTree:
    """Multi-leaf B+Tree backed by a Pager.

    The root_page_id always points to the root page. The root page may be
    either a leaf page (single-leaf tree) or an internal root page
    (multi-leaf tree).
    """

    def __init__(self, pager, root_page_id=None):
        self._pager = pager
        self._cells: list[tuple[int, bytes]] = []
        self._children: list[tuple[int, int]] = []
        self._is_leaf = True

        if root_page_id is None:
            self._root_page_id = self._pager.allocate_page()
            self._init_leaf()
        else:
            self._root_page_id = root_page_id
            self._load_root()

    @property
    def root_page_id(self) -> int:
        return self._root_page_id

    def _init_leaf(self) -> None:
        """Initialize a fresh empty leaf page as root."""
        page = _build_leaf_page([])
        self._pager.write_page(self._root_page_id, page)

    def _load_root(self) -> None:
        """Load the root page. Detects leaf vs internal root."""
        raw = self._pager.read_page(self._root_page_id)
        if len(raw) < 4:
            raise CorruptDatabaseError("Root page too short")
        magic = raw[0:4]
        if magic == LEAF_MAGIC:
            self._is_leaf = True
            self._cells = _load_cells_from_page(raw)
        elif magic == INTERNAL_MAGIC:
            self._is_leaf = False
            self._children = _load_internal_root(raw)
        else:
            raise CorruptDatabaseError(
                f"Invalid root page magic: {magic!r}"
            )

    def _is_internal_root(self) -> bool:
        """Check if the root page is an internal root page."""
        if self._is_leaf:
            return False
        raw = self._pager.read_page(self._root_page_id)
        magic = raw[0:4]
        return magic == INTERNAL_MAGIC

    def _get_leaf_for_rowid(self, rowid: int) -> int:
        """Find the leaf page id that should contain the given rowid.

        Traverses internal root pages down to a leaf.
        """
        return self._find_leaf_recursive(self._root_page_id, rowid)

    def _find_leaf_recursive(self, page_id: int, rowid: int) -> int:
        """Recursively find the leaf page id for a given rowid."""
        raw = self._pager.read_page(page_id)
        magic = raw[0:4]
        if magic == LEAF_MAGIC:
            return page_id
        elif magic == INTERNAL_MAGIC:
            children = _load_internal_root(raw)
            # Find the right child: the first child whose max_key >= rowid
            target_child = children[-1][1]  # default to last child
            for max_key, child_page_id in children:
                if max_key >= rowid:
                    target_child = child_page_id
                    break
            return self._find_leaf_recursive(target_child, rowid)
        else:
            raise CorruptDatabaseError(
                f"Invalid page magic: {magic!r}"
            )

    def _scan_all_leaves(self) -> list[tuple[int, bytes]]:
        """Scan all rows from all leaf pages in rowid ascending order."""
        if self._is_leaf:
            return list(self._cells)

        # Read internal root to get all leaf pages
        raw = self._pager.read_page(self._root_page_id)
        children = _load_internal_root(raw)

        all_rows = []
        for max_key, child_page_id in children:
            leaf_rows = self._scan_leaf_page(child_page_id)
            all_rows.extend(leaf_rows)

        all_rows.sort(key=lambda r: r[0])
        return all_rows

    def _scan_leaf_page(self, page_id: int) -> list[tuple[int, bytes]]:
        """Scan all rows from a single leaf page."""
        raw = self._pager.read_page(page_id)
        return _load_cells_from_page(raw)

    def _repack_tree(self, cells: list[tuple[int, bytes]]) -> None:
        """Repack the tree with the given sorted cells.

        If cells fit in one page, use a single leaf root.
        Otherwise, split into multiple leaf pages and create an internal root.
        """
        # Ensure cells are sorted
        cells = sorted(cells, key=lambda c: c[0])

        if _cells_fit_in_page(cells):
            # Single leaf root
            self._is_leaf = True
            self._cells = cells
            page = _build_leaf_page(cells)
            self._pager.write_page(self._root_page_id, page)
        else:
            # Multiple leaf pages needed
            self._is_leaf = False
            self._cells = []
            self._children = []

            # Split cells into leaf-sized chunks
            leaf_pages = []
            current_chunk = []
            current_size = LEAF_HEADER_SIZE

            for rowid, payload in cells:
                cell_size = 12 + len(payload)  # rowid(8) + payload_len(4) + payload
                if current_size + cell_size > (PAGE_SIZE - 4) and current_chunk:
                    # Flush current chunk to a leaf page
                    leaf_page_id = self._pager.allocate_page()
                    leaf_page = _build_leaf_page(current_chunk)
                    self._pager.write_page(leaf_page_id, leaf_page)
                    leaf_pages.append((current_chunk[-1][0], leaf_page_id))
                    current_chunk = []
                    current_size = LEAF_HEADER_SIZE

                current_chunk.append((rowid, payload))
                current_size += cell_size

            # Flush remaining chunk
            if current_chunk:
                leaf_page_id = self._pager.allocate_page()
                leaf_page = _build_leaf_page(current_chunk)
                self._pager.write_page(leaf_page_id, leaf_page)
                leaf_pages.append((current_chunk[-1][0], leaf_page_id))

            # Build internal root page
            internal_page = _build_internal_root_page(leaf_pages)
            self._pager.write_page(self._root_page_id, internal_page)

    def search(self, rowid: int):
        """Search for a rowid. Returns payload bytes or None."""
        if self._is_leaf:
            for r, p in self._cells:
                if r == rowid:
                    return p
                if r > rowid:
                    break
            return None

        # Traverse internal root to find the right leaf
        leaf_page_id = self._get_leaf_for_rowid(rowid)
        raw = self._pager.read_page(leaf_page_id)
        cells = _load_cells_from_page(raw)
        for r, p in cells:
            if r == rowid:
                return p
            if r > rowid:
                break
        return None

    def insert(self, rowid: int, payload: bytes) -> None:
        """Insert a (rowid, payload) pair.

        Raises DuplicateKeyError if rowid already exists.
        """
        # Check for duplicate
        if self._is_leaf:
            for r, _ in self._cells:
                if r == rowid:
                    raise DuplicateKeyError(f"Duplicate rowid: {rowid}")
                if r > rowid:
                    break
        else:
            # Check all leaves for duplicate
            raw = self._pager.read_page(self._root_page_id)
            children = _load_internal_root(raw)
            for max_key, child_page_id in children:
                leaf_raw = self._pager.read_page(child_page_id)
                leaf_cells = _load_cells_from_page(leaf_raw)
                for r, _ in leaf_cells:
                    if r == rowid:
                        raise DuplicateKeyError(f"Duplicate rowid: {rowid}")
                    if r > rowid:
                        break

        # Get all rows, insert, and repack
        all_rows = self._scan_all_leaves()

        # Insert in sorted order
        inserted = False
        for i, (r, _) in enumerate(all_rows):
            if r > rowid:
                all_rows.insert(i, (rowid, payload))
                inserted = True
                break
        if not inserted:
            all_rows.append((rowid, payload))

        self._repack_tree(all_rows)

    def delete(self, rowid: int) -> bool:
        """Delete a rowid. Returns True if found and deleted, False otherwise."""
        # Get all rows
        all_rows = self._scan_all_leaves()

        # Find and remove the row
        for i, (r, _) in enumerate(all_rows):
            if r == rowid:
                all_rows.pop(i)
                self._repack_tree(all_rows)
                return True
            if r > rowid:
                break
        return False

    def scan_all(self) -> list[tuple[int, bytes]]:
        """Scan all cells in rowid ascending order."""
        return self._scan_all_leaves()
