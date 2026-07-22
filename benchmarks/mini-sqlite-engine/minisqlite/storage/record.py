"""Binary record codec for Mini SQLite Engine.

Record format:
    column_count: 2 bytes unsigned big-endian
    value_1
    value_2
    ...
    value_n

Value encodings:
    NULL:     type byte 0x00
    INTEGER:  type byte 0x01, then 8-byte signed big-endian
    TEXT:     type byte 0x02, then 4-byte unsigned big-endian length, then UTF-8 bytes
"""

import struct
from minisqlite.errors import CorruptDatabaseError

# Type tags
NULL_TYPE = 0
INTEGER_TYPE = 1
TEXT_TYPE = 2

# Sizes
INTEGER_SIZE = 8
TEXT_HEADER_SIZE = 4


def encode_record(values: list) -> bytes:
    """Encode a list of Python values into a binary record.

    Supported value types:
        - None -> NULL
        - int -> INTEGER (8-byte signed big-endian)
        - str -> TEXT (4-byte length prefix + UTF-8 bytes)

    Raises:
        TypeError: If a value type is not supported.
    """
    parts = [struct.pack(">H", len(values))]  # column count

    for value in values:
        if value is None:
            parts.append(struct.pack("B", NULL_TYPE))
        elif isinstance(value, int):
            parts.append(struct.pack("B", INTEGER_TYPE))
            parts.append(struct.pack(">q", value))
        elif isinstance(value, str):
            encoded = value.encode("utf-8")
            parts.append(struct.pack("B", TEXT_TYPE))
            parts.append(struct.pack(">I", len(encoded)))
            parts.append(encoded)
        else:
            raise TypeError(
                f"Unsupported value type: {type(value).__name__}. "
                "Supported: None, int, str."
            )

    return b"".join(parts)


def decode_record(data: bytes) -> list:
    """Decode a binary record into a list of Python values.

    Raises:
        CorruptDatabaseError: If the payload is truncated or malformed.
    """
    if len(data) < 2:
        raise CorruptDatabaseError(
            "Truncated record: cannot read column count"
        )

    column_count = struct.unpack(">H", data[0:2])[0]
    offset = 2
    result = []

    for _ in range(column_count):
        if offset >= len(data):
            raise CorruptDatabaseError(
                "Truncated record: missing type byte"
            )

        type_tag = data[offset]
        offset += 1

        if type_tag == NULL_TYPE:
            result.append(None)

        elif type_tag == INTEGER_TYPE:
            if offset + INTEGER_SIZE > len(data):
                raise CorruptDatabaseError(
                    "Truncated record: incomplete INTEGER value"
                )
            value = struct.unpack(">q", data[offset : offset + INTEGER_SIZE])[0]
            offset += INTEGER_SIZE
            result.append(value)

        elif type_tag == TEXT_TYPE:
            if offset + TEXT_HEADER_SIZE > len(data):
                raise CorruptDatabaseError(
                    "Truncated record: incomplete TEXT length header"
                )
            text_length = struct.unpack(">I", data[offset : offset + TEXT_HEADER_SIZE])[0]
            offset += TEXT_HEADER_SIZE

            if offset + text_length > len(data):
                raise CorruptDatabaseError(
                    "Truncated record: incomplete TEXT payload"
                )
            value = data[offset : offset + text_length].decode("utf-8")
            offset += text_length
            result.append(value)

        else:
            raise CorruptDatabaseError(
                f"Unknown type tag: {type_tag}"
            )

    return result
