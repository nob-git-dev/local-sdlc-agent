"""Fixed file header and page 0 layout for MiniSQLite Engine.

This module implements the fixed-size database header described in
SPEC.md section 11.3.  Page 0 is the header-only page and is always
PAGE_SIZE bytes long.  All integers in the header are big-endian.
"""

import struct

from minisqlite.errors import CorruptDatabaseError

MAGIC = b"MSQLITE1"
PAGE_SIZE = 4096
FORMAT_VERSION = 1
HEADER_SIZE = 32

# Header field offsets (SPEC.md section 11.3).
_MAGIC_OFFSET = 0
_PAGE_SIZE_OFFSET = 8
_FORMAT_VERSION_OFFSET = 12
_NEXT_PAGE_ID_OFFSET = 16
_SCHEMA_ROOT_PAGE_OFFSET = 20
_RESERVED_OFFSET = 24

# struct format for the fixed 32-byte header: magic(8) + 4 uint32 + 8 reserved.
_HEADER_STRUCT = struct.Struct(">8s4I8s")


def create_header(next_page_id: int = 1, schema_root_page: int = 0) -> bytes:
    """Return the 32-byte big-endian header for a new database.

    A fresh database has no pages beyond page 0, so next_page_id starts
    at 1.  schema_root_page is 0 until a schema B+Tree page is created.
    """
    if next_page_id < 1:
        raise CorruptDatabaseError("next_page_id must be at least 1")
    if schema_root_page < 0:
        raise CorruptDatabaseError("schema_root_page must be non-negative")
    return _HEADER_STRUCT.pack(
        MAGIC,
        PAGE_SIZE,
        FORMAT_VERSION,
        next_page_id,
        schema_root_page,
        b"\x00" * 8,
    )


def create_page0(next_page_id: int = 1, schema_root_page: int = 0) -> bytes:
    """Return a complete 4096-byte page 0 containing the header.

    The header occupies the first 32 bytes; the remaining bytes are
    zero-filled free space.
    """
    header = create_header(next_page_id, schema_root_page)
    return header + b"\x00" * (PAGE_SIZE - HEADER_SIZE)


def parse_header(page0: bytes) -> dict:
    """Parse a full page 0 and return its header fields.

    Raises CorruptDatabaseError if the page is not exactly PAGE_SIZE
    bytes or the magic bytes do not match.
    """
    if len(page0) != PAGE_SIZE:
        raise CorruptDatabaseError(
            "page 0 must be exactly %d bytes, got %d" % (PAGE_SIZE, len(page0))
        )
    header = page0[:_HEADER_STRUCT.size]
    magic, page_size, format_version, next_page_id, schema_root_page, _reserved = (
        _HEADER_STRUCT.unpack(header)
    )
    if magic != MAGIC:
        raise CorruptDatabaseError("invalid magic bytes in database header")
    return {
        "magic": magic,
        "page_size": page_size,
        "format_version": format_version,
        "next_page_id": next_page_id,
        "schema_root_page": schema_root_page,
    }


def validate_page0(page0: bytes) -> None:
    """Validate page 0 and raise CorruptDatabaseError on any violation."""
    parsed = parse_header(page0)
    if parsed["page_size"] != PAGE_SIZE:
        raise CorruptDatabaseError(
            "page_size mismatch: expected %d, got %d" % (PAGE_SIZE, parsed["page_size"])
        )
    if parsed["format_version"] != FORMAT_VERSION:
        raise CorruptDatabaseError(
            "format_version mismatch: expected %d, got %d"
            % (FORMAT_VERSION, parsed["format_version"])
        )
    if parsed["next_page_id"] < 1:
        raise CorruptDatabaseError("next_page_id must be at least 1")
    if parsed["schema_root_page"] < 0:
        raise CorruptDatabaseError("schema_root_page must be non-negative")