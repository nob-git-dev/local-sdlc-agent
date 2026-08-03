"""B+Tree storage layer for MiniSQLite Engine.

S07 implements a persistent rowid-keyed B+Tree over the 4096-byte Pager.
The tree supports search, insert, scan_all, and delete.  Leaf cells are
ordered by rowid and leaves carry a right-sibling link.  When a leaf is
full, it is split roughly in half and an internal root is created on the
first split.  All integers are big-endian.
"""

import struct

from minisqlite.errors import DuplicateKeyError
from minisqlite.storage.file_format import PAGE_SIZE

# Leaf page layout:
#   offset 0:  uint32  number of cells
#   offset 4:  uint32  right-sibling page id (0 = none)
#   offset 8:  uint32  reserved
#   offset 12: uint32  reserved
#   offset 16: cell array (rowid 8 bytes + payload size 4 bytes + payload)
_LEAF_HEADER_SIZE = 16
_CELL_HEADER_SIZE = 12  # rowid (8) + payload size (4)
_ROWID_STRUCT = struct.Struct(">q")
_PAYLOAD_SIZE_STRUCT = struct.Struct(">I")
_HEADER_STRUCT = struct.Struct(">4I")

# Internal page layout:
#   offset 0:  uint32  INTERNAL_MAGIC
#   offset 4:  uint32  number of children
#   offset 8:  uint32  reserved
#   offset 12: uint32  reserved
#   offset 16: child array (left child page id 4 bytes + boundary rowid 8 bytes)
_INTERNAL_HEADER_SIZE = 16
_CHILD_HEADER_SIZE = 12  # child page id (4) + boundary rowid (8)
_PAGE_ID_STRUCT = struct.Struct(">I")
INTERNAL_MAGIC = b"INTD"
_INTERNAL_HEADER_STRUCT = struct.Struct(">4sIII")


def _encode_leaf_header(cell_count, right_sibling):
    """Encode the 16-byte leaf header as big-endian integers."""
    return _HEADER_STRUCT.pack(cell_count, right_sibling, 0, 0)


def _decode_leaf_header(page):
    """Decode the 16-byte leaf header from a page.

    Returns (cell_count, right_sibling).
    """
    cell_count, right_sibling, _reserved1, _reserved2 = _HEADER_STRUCT.unpack(
        page[0:_LEAF_HEADER_SIZE]
    )
    return cell_count, right_sibling


def _encode_cell(rowid, payload):
    """Encode a single leaf cell as rowid + payload size + payload."""
    return _ROWID_STRUCT.pack(rowid) + _PAYLOAD_SIZE_STRUCT.pack(len(payload)) + payload


def _decode_cell(data):
    """Decode a single leaf cell into (rowid, payload)."""
    rowid = _ROWID_STRUCT.unpack(data[0:8])[0]
    payload_size = _PAYLOAD_SIZE_STRUCT.unpack(data[8:12])[0]
    payload = data[12 : 12 + payload_size]
    return rowid, payload


def _encode_internal_header(child_count):
    """Encode the 16-byte internal header as big-endian integers."""
    return _INTERNAL_HEADER_STRUCT.pack(INTERNAL_MAGIC, child_count, 0, 0)


def _decode_internal_header(page):
    """Decode the 16-byte internal header from a page.

    Returns (is_internal, child_count).
    """
    magic, child_count, _reserved1, _reserved2 = _INTERNAL_HEADER_STRUCT.unpack(
        page[0:_INTERNAL_HEADER_SIZE]
    )
    return magic == INTERNAL_MAGIC, child_count


def _encode_child(left_child, boundary_rowid):
    """Encode a single internal child entry as child page id + boundary rowid."""
    return _PAGE_ID_STRUCT.pack(left_child) + _ROWID_STRUCT.pack(boundary_rowid)


def _decode_child(data):
    """Decode a single internal child entry into (left_child, boundary_rowid)."""
    left_child = _PAGE_ID_STRUCT.unpack(data[0:4])[0]
    boundary_rowid = _ROWID_STRUCT.unpack(data[4:12])[0]
    return left_child, boundary_rowid


class BTree:
    """A persistent rowid-keyed B+Tree over the 4096-byte Pager."""

    def __init__(self, pager, root_page_id=None):
        self._pager = pager
        if root_page_id is None:
            self._root_page_id = pager.allocate_page()
        else:
            self._root_page_id = root_page_id

    @property
    def root_page_id(self):
        """Return the root page id of this tree."""
        return self._root_page_id

    def _read_leaf(self, page_id):
        """Read and decode a leaf page."""
        page = self._pager.read_page(page_id)
        cell_count, right_sibling = _decode_leaf_header(page)
        cells = []
        offset = _LEAF_HEADER_SIZE
        for _ in range(cell_count):
            rowid = _ROWID_STRUCT.unpack(page[offset : offset + 8])[0]
            payload_size = _PAYLOAD_SIZE_STRUCT.unpack(page[offset + 8 : offset + 12])[0]
            payload = page[offset + 12 : offset + 12 + payload_size]
            cells.append((rowid, payload))
            offset += _CELL_HEADER_SIZE + payload_size
        return cells, right_sibling

    def _write_leaf(self, page_id, cells, right_sibling):
        """Encode and persist a leaf page from an ordered cell list."""
        body = b"".join(_encode_cell(rowid, payload) for rowid, payload in cells)
        page = _encode_leaf_header(len(cells), right_sibling) + body
        page = page.ljust(PAGE_SIZE, b"\x00")
        self._pager.write_page(page_id, page)

    def _read_internal(self, page_id):
        """Read and decode an internal page into child entries."""
        page = self._pager.read_page(page_id)
        is_internal, child_count = _decode_internal_header(page)
        if not is_internal:
            raise ValueError("page %d is not an internal page" % page_id)
        children = []
        offset = _INTERNAL_HEADER_SIZE
        for _ in range(child_count):
            left_child = _PAGE_ID_STRUCT.unpack(page[offset : offset + 4])[0]
            boundary_rowid = _ROWID_STRUCT.unpack(page[offset + 4 : offset + 12])[0]
            children.append((left_child, boundary_rowid))
            offset += _CHILD_HEADER_SIZE
        return children

    def _write_internal(self, page_id, children):
        """Encode and persist an internal page from child entries."""
        body = b"".join(_encode_child(left, boundary) for left, boundary in children)
        page = _encode_internal_header(len(children)) + body
        page = page.ljust(PAGE_SIZE, b"\x00")
        self._pager.write_page(page_id, page)

    def _is_internal(self, page_id):
        """Return True if the page at page_id is an internal page."""
        page = self._pager.read_page(page_id)
        return page[0:4] == INTERNAL_MAGIC
    def _find_leaf(self, rowid):
        """Return the leaf page id that should contain rowid."""
        page_id = self._root_page_id
        while self._is_internal(page_id):
            children = self._read_internal(page_id)
            for index, (left_child, boundary_rowid) in enumerate(children):
                if rowid <= boundary_rowid:
                    page_id = left_child
                    break
            else:
                # rowid is greater than the last boundary; use the last child
                page_id = children[-1][0]
        return page_id

    def _split_leaf(self, page_id, cells, right_sibling):
        """Split a full leaf into two leaves and return (left, right, boundary).

        The left leaf keeps the first half of the cells, the right leaf keeps
        the second half.  The right leaf inherits the original right sibling.
        The boundary rowid is the first rowid of the right leaf.
        """
        mid = len(cells) // 2
        left_cells = cells[:mid]
        right_cells = cells[mid:]
        left_max_rowid = left_cells[-1][0]
        right_max_rowid = right_cells[-1][0]
        new_right_page = self._pager.allocate_page()
        self._write_leaf(page_id, left_cells, new_right_page)
        self._write_leaf(new_right_page, right_cells, right_sibling)
        return page_id, new_right_page, left_max_rowid, right_max_rowid

    def _insert_into_internal(self, page_id, split_page_id, left_page, right_page, left_max, right_max):
        """Replace the entry for split_page_id with two new child entries.

        Returns page_id unchanged.
        """
        children = self._read_internal(page_id)
        found_index = None
        for index, (existing_left, existing_boundary) in enumerate(children):
            if existing_left == split_page_id:
                found_index = index
                break
        if found_index is None:
            raise ValueError("split page %d not found" % split_page_id)
        children[found_index] = (left_page, left_max)
        children.insert(found_index + 1, (right_page, right_max))
        children.sort(key=lambda child: child[1])
        body = b"".join(_encode_child(left, boundary) for left, boundary in children)
        if _INTERNAL_HEADER_SIZE + len(body) > PAGE_SIZE:
            raise ValueError("internal page %d overflow" % page_id)
        self._write_internal(page_id, children)
        return page_id

    def search(self, rowid):
        """Return the payload for rowid, or None if it does not exist."""
        leaf_id = self._find_leaf(rowid)
        cells, _right_sibling = self._read_leaf(leaf_id)
        for existing_rowid, payload in cells:
            if existing_rowid == rowid:
                return payload
            if existing_rowid > rowid:
                return None
        return None

    def insert(self, rowid, payload):
        """Insert a rowid/payload pair into the tree.

        Raises DuplicateKeyError if the rowid already exists.
        Raises ValueError if the payload cannot fit in an empty leaf.
        """
        if not isinstance(payload, bytes):
            raise TypeError("payload must be bytes")
        leaf_id = self._find_leaf(rowid)
        cells, right_sibling = self._read_leaf(leaf_id)
        for index, (existing_rowid, _existing_payload) in enumerate(cells):
            if existing_rowid == rowid:
                raise DuplicateKeyError("rowid %d already exists" % rowid)
            if existing_rowid > rowid:
                break
        else:
            index = len(cells)
        new_cells = cells[:index] + [(rowid, payload)] + cells[index:]
        encoded = b"".join(_encode_cell(r, p) for r, p in new_cells)
        if _LEAF_HEADER_SIZE + len(_encode_cell(rowid, payload)) > PAGE_SIZE:
            raise ValueError("payload too large for a single cell")
        if _LEAF_HEADER_SIZE + len(encoded) > PAGE_SIZE:
            # The leaf is full; split it roughly in half.
            left_page, right_page, left_max, right_max = self._split_leaf(
                leaf_id, new_cells, right_sibling
            )
            if self._is_internal(self._root_page_id):
                self._root_page_id = self._insert_into_internal(
                    self._root_page_id, leaf_id, left_page, right_page, left_max, right_max
                )
            else:
                # First split: create a new internal root.
                new_root = self._pager.allocate_page()
                self._write_internal(new_root, [(left_page, left_max), (right_page, right_max)])
                self._root_page_id = new_root
            return
        self._write_leaf(leaf_id, new_cells, right_sibling)

    def scan_all(self):
        """Return a list of (rowid, payload) pairs in ascending rowid order."""
        page_id = self._root_page_id
        while self._is_internal(page_id):
            children = self._read_internal(page_id)
            page_id = children[0][0]
        result = []
        while True:
            cells, right_sibling = self._read_leaf(page_id)
            for rowid, payload in cells:
                result.append((rowid, payload))
            if right_sibling == 0:
                break
            page_id = right_sibling
        return result

    def delete(self, rowid):
        """Remove rowid from the tree.

        Returns True if the rowid was removed, False if it did not exist.
        """
        leaf_id = self._find_leaf(rowid)
        cells, right_sibling = self._read_leaf(leaf_id)
        for index, (existing_rowid, _existing_payload) in enumerate(cells):
            if existing_rowid == rowid:
                new_cells = cells[:index] + cells[index + 1 :]
                self._write_leaf(leaf_id, new_cells, right_sibling)
                return True
            if existing_rowid > rowid:
                return False
        return False
