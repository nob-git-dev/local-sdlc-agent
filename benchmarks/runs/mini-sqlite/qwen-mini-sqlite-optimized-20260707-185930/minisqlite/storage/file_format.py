"""
Database File Format Definitions

Defines constants and structures for the MiniSQLite database file format.
"""

from dataclasses import dataclass
from typing import Optional

# Magic bytes for database file validation
MAGIC_BYTES = b"MSQLITE1"
MAGIC_SIZE = 8

# Page size (fixed)
PAGE_SIZE = 4096

# Format version
FORMAT_VERSION = 1

# Page types for B+Tree
PAGE_TYPE_LEAF = 1
PAGE_TYPE_INTERNAL = 2

# Header page offsets
HEADER_MAGIC_OFFSET = 0
HEADER_MAGIC_SIZE = 8
HEADER_PAGE_SIZE_OFFSET = 8
HEADER_PAGE_SIZE_SIZE = 4
HEADER_FORMAT_VERSION_OFFSET = 12
HEADER_FORMAT_VERSION_SIZE = 4
HEADER_NEXT_PAGE_ID_OFFSET = 16
HEADER_NEXT_PAGE_ID_SIZE = 4
HEADER_SCHEMA_ROOT_PAGE_OFFSET = 20
HEADER_SCHEMA_ROOT_PAGE_SIZE = 4
HEADER_RESERVED_OFFSET = 24
HEADER_RESERVED_SIZE = 8
HEADER_SCHEMA_START_OFFSET = 32

# Header size (first 32 bytes are fixed)
HEADER_FIXED_SIZE = 32


@dataclass
class DatabaseHeader:
    """Database file header structure."""
    magic: bytes
    page_size: int
    format_version: int
    next_page_id: int
    schema_root_page: int
    reserved: bytes

    def __post_init__(self):
        if len(self.magic) != MAGIC_SIZE:
            raise ValueError(f"Magic must be {MAGIC_SIZE} bytes, got {len(self.magic)}")
        if len(self.reserved) != HEADER_RESERVED_SIZE:
            raise ValueError(f"Reserved must be {HEADER_RESERVED_SIZE} bytes, got {len(self.reserved)}")

    @classmethod
    def from_bytes(cls, data: bytes) -> "DatabaseHeader":
        """Deserialize header from bytes."""
        if len(data) < HEADER_FIXED_SIZE:
            raise ValueError(f"Header data must be at least {HEADER_FIXED_SIZE} bytes")

        magic = data[HEADER_MAGIC_OFFSET:HEADER_MAGIC_OFFSET + HEADER_MAGIC_SIZE]
        page_size = int.from_bytes(
            data[HEADER_PAGE_SIZE_OFFSET:HEADER_PAGE_SIZE_OFFSET + HEADER_PAGE_SIZE_SIZE],
            byteorder="big",
            signed=False
        )
        format_version = int.from_bytes(
            data[HEADER_FORMAT_VERSION_OFFSET:HEADER_FORMAT_VERSION_OFFSET + HEADER_FORMAT_VERSION_SIZE],
            byteorder="big",
            signed=False
        )
        next_page_id = int.from_bytes(
            data[HEADER_NEXT_PAGE_ID_OFFSET:HEADER_NEXT_PAGE_ID_OFFSET + HEADER_NEXT_PAGE_ID_SIZE],
            byteorder="big",
            signed=False
        )
        schema_root_page = int.from_bytes(
            data[HEADER_SCHEMA_ROOT_PAGE_OFFSET:HEADER_SCHEMA_ROOT_PAGE_OFFSET + HEADER_SCHEMA_ROOT_PAGE_SIZE],
            byteorder="big",
            signed=False
        )
        reserved = data[HEADER_RESERVED_OFFSET:HEADER_RESERVED_OFFSET + HEADER_RESERVED_SIZE]

        return cls(
            magic=magic,
            page_size=page_size,
            format_version=format_version,
            next_page_id=next_page_id,
            schema_root_page=schema_root_page,
            reserved=reserved
        )

    def to_bytes(self) -> bytes:
        """Serialize header to bytes."""
        result = bytearray(HEADER_FIXED_SIZE)

        # Magic bytes
        result[HEADER_MAGIC_OFFSET:HEADER_MAGIC_OFFSET + MAGIC_SIZE] = self.magic

        # Page size (4 bytes, big-endian)
        result[HEADER_PAGE_SIZE_OFFSET:HEADER_PAGE_SIZE_OFFSET + HEADER_PAGE_SIZE_SIZE] = (
            self.page_size.to_bytes(HEADER_PAGE_SIZE_SIZE, byteorder="big", signed=False)
        )

        # Format version (4 bytes, big-endian)
        result[HEADER_FORMAT_VERSION_OFFSET:HEADER_FORMAT_VERSION_OFFSET + HEADER_FORMAT_VERSION_SIZE] = (
            self.format_version.to_bytes(HEADER_FORMAT_VERSION_SIZE, byteorder="big", signed=False)
        )

        # Next page ID (4 bytes, big-endian)
        result[HEADER_NEXT_PAGE_ID_OFFSET:HEADER_NEXT_PAGE_ID_OFFSET + HEADER_NEXT_PAGE_ID_SIZE] = (
            self.next_page_id.to_bytes(HEADER_NEXT_PAGE_ID_SIZE, byteorder="big", signed=False)
        )

        # Schema root page (4 bytes, big-endian)
        result[HEADER_SCHEMA_ROOT_PAGE_OFFSET:HEADER_SCHEMA_ROOT_PAGE_OFFSET + HEADER_SCHEMA_ROOT_PAGE_SIZE] = (
            self.schema_root_page.to_bytes(HEADER_SCHEMA_ROOT_PAGE_SIZE, byteorder="big", signed=False)
        )

        # Reserved (8 bytes)
        result[HEADER_RESERVED_OFFSET:HEADER_RESERVED_OFFSET + HEADER_RESERVED_SIZE] = self.reserved

        return bytes(result)


@dataclass
class PageHeader:
    """B+Tree page header structure (common for leaf and internal pages)."""
    page_type: int
    is_root: bool
    parent_page_id: Optional[int] = None

    def to_bytes(self, is_leaf: bool = True) -> bytes:
        """Serialize page header to bytes."""
        result = bytearray(16)

        # Page type (1 byte)
        result[0] = self.page_type

        # Is root flag (1 byte)
        result[1] = 1 if self.is_root else 0

        # Parent page ID (4 bytes, big-endian, 0 if not applicable)
        parent_id = self.parent_page_id if self.parent_page_id is not None else 0
        result[4:8] = parent_id.to_bytes(4, byteorder="big", signed=False)

        # Reserved (8 bytes)
        # For leaf pages, offset 8-11 is right_sibling_page_id
        # For internal pages, offset 8-11 is rightmost_child_page_id
        # We'll set this to 0 for now
        result[8:12] = b"\x00\x00\x00\x00"

        # Cell area start (2 bytes, 16 for header)
        result[12:14] = (16).to_bytes(2, byteorder="big", signed=False)

        # Reserved (2 bytes)
        result[14:16] = b"\x00\x00"

        return bytes(result)

    @classmethod
    def from_bytes(cls, data: bytes, is_leaf: bool = True) -> "PageHeader":
        """Deserialize page header from bytes."""
        if len(data) < 16:
            raise ValueError(f"Page header must be at least 16 bytes, got {len(data)}")

        page_type = data[0]
        is_root = data[1] == 1
        parent_page_id = int.from_bytes(data[4:8], byteorder="big", signed=False)

        # Convert 0 to None for parent_page_id
        if parent_page_id == 0:
            parent_page_id = None

        return cls(
            page_type=page_type,
            is_root=is_root,
            parent_page_id=parent_page_id
        )