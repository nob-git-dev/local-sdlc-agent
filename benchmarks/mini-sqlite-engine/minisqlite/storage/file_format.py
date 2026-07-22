"""Mini SQLite Engine file format constants and header helpers."""

import struct

PAGE_SIZE = 4096

MAGIC = b"MSQLITE1"
FORMAT_VERSION = 1

HEADER_FORMAT = struct.Struct(">8sIIIIq")
HEADER_SIZE = HEADER_FORMAT.size  # 32


def build_header(next_page_id: int = 1, schema_root_page: int = 0) -> bytes:
    """Build a 32-byte header page."""
    return HEADER_FORMAT.pack(
        MAGIC,
        PAGE_SIZE,
        FORMAT_VERSION,
        next_page_id,
        schema_root_page,
        0,  # reserved
    )


def parse_header(data: bytes):
    """Parse a 32-byte header into a dict.

    Raises ValueError if the data is too short.
    """
    if len(data) < HEADER_SIZE:
        raise ValueError("Header too short")
    magic, page_size, format_version, next_page_id, schema_root_page, reserved = (
        HEADER_FORMAT.unpack(data[:HEADER_SIZE])
    )
    return {
        "magic": magic,
        "page_size": page_size,
        "format_version": format_version,
        "next_page_id": next_page_id,
        "schema_root_page": schema_root_page,
        "reserved": reserved,
    }
