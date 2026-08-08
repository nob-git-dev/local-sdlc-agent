"""Record codec for MiniSQLite storage layer.

Implements SPEC.md section 13 record encoding:

- column_count: 2 bytes (big-endian unsigned short)
- followed by values in column order

Value type tags (SPEC.md section 13.3):
- NULL = 0: 1 byte type only (no payload)
- INTEGER = 1: 1 byte type + 8 bytes signed big-endian
- TEXT = 2: 1 byte type + 4 bytes length + UTF-8 bytes

Corrupt payloads raise CorruptDatabaseError (SPEC.md section 15.1).
"""

import struct

from minisqlite.errors import CorruptDatabaseError

_TYPE_NULL = 0
_TYPE_INTEGER = 1
_TYPE_TEXT = 2

_INTEGER_SIZE = 8
_TEXT_LENGTH_SIZE = 4
_COLUMN_COUNT_SIZE = 2


def encode_record(values):
    """Encode a list of column values into SPEC.md section 13 record bytes.

    Supported value types:
    - None (NULL)
    - int (64-bit signed, excluding bool)
    - str (UTF-8 text)

    Unsupported types raise TypeError.
    """
    if not isinstance(values, list):
        raise TypeError("values must be a list")

    column_count = len(values)
    if column_count > 0xFFFF:
        raise ValueError("column count exceeds 16-bit range")

    parts = [struct.pack(">H", column_count)]

    for value in values:
        if value is None:
            parts.append(struct.pack(">B", _TYPE_NULL))
        elif isinstance(value, bool):
            raise TypeError("bool is not a supported record value type")
        elif isinstance(value, int):
            if value < -(1 << 63) or value > (1 << 63) - 1:
                raise ValueError("integer out of 64-bit signed range")
            parts.append(struct.pack(">B", _TYPE_INTEGER))
            parts.append(struct.pack(">q", value))
        elif isinstance(value, str):
            encoded = value.encode("utf-8")
            parts.append(struct.pack(">B", _TYPE_TEXT))
            parts.append(struct.pack(">I", len(encoded)))
            parts.append(encoded)
        else:
            raise TypeError(
                "unsupported value type: {0}".format(type(value).__name__)
            )

    return b"".join(parts)


def decode_record(data):
    """Decode SPEC.md section 13 record bytes back into a list of values.

    Raises CorruptDatabaseError for malformed payloads:
    - truncated data
    - invalid type tag
    - invalid UTF-8
    - trailing bytes after the record
    """
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")

    if len(data) < _COLUMN_COUNT_SIZE:
        raise CorruptDatabaseError("record header truncated")

    column_count = struct.unpack(">H", data[0:_COLUMN_COUNT_SIZE])[0]
    offset = _COLUMN_COUNT_SIZE

    values = []

    for _ in range(column_count):
        if offset >= len(data):
            raise CorruptDatabaseError("record value type tag truncated")

        type_tag = data[offset]
        offset += 1

        if type_tag == _TYPE_NULL:
            values.append(None)
        elif type_tag == _TYPE_INTEGER:
            if offset + _INTEGER_SIZE > len(data):
                raise CorruptDatabaseError("integer payload truncated")
            value = struct.unpack(">q", data[offset:offset + _INTEGER_SIZE])[0]
            offset += _INTEGER_SIZE
            values.append(value)
        elif type_tag == _TYPE_TEXT:
            if offset + _TEXT_LENGTH_SIZE > len(data):
                raise CorruptDatabaseError("text length truncated")
            text_length = struct.unpack(
                ">I", data[offset:offset + _TEXT_LENGTH_SIZE]
            )[0]
            offset += _TEXT_LENGTH_SIZE
            if offset + text_length > len(data):
                raise CorruptDatabaseError("text payload truncated")
            raw = data[offset:offset + text_length]
            offset += text_length
            try:
                values.append(raw.decode("utf-8"))
            except UnicodeDecodeError:
                raise CorruptDatabaseError("invalid UTF-8 in text value")
        else:
            raise CorruptDatabaseError(
                "invalid type tag: {0}".format(type_tag)
            )

    if offset != len(data):
        raise CorruptDatabaseError("trailing bytes after record")

    return values