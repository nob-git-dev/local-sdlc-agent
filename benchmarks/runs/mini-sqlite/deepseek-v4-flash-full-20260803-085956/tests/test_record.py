"""Tests for the record codec (SPEC.md section 13).

Covers SPEC.md section 16.1 Record Codec tests:
- INTEGER roundtrip
- TEXT roundtrip
- multi-column roundtrip
- UTF-8 Japanese roundtrip
- corrupt payload detection
"""

import struct
import unittest

from minisqlite.errors import CorruptDatabaseError
from minisqlite.storage.record import decode_record, encode_record

_TYPE_NULL = 0
_TYPE_INTEGER = 1
_TYPE_TEXT = 2

_INTEGER_SIZE = 8
_TEXT_LENGTH_SIZE = 4
_COLUMN_COUNT_SIZE = 2


class EncodeRecordTest(unittest.TestCase):
    """Tests for encode_record."""

    def test_encode_none(self):
        """None encodes as column_count + NULL type tag only."""
        encoded = encode_record([None])
        self.assertEqual(encoded, struct.pack(">H", 1) + struct.pack(">B", _TYPE_NULL))

    def test_encode_positive_integer(self):
        """Positive integer encodes as type tag + 8-byte big-endian."""
        encoded = encode_record([42])
        self.assertEqual(
            encoded,
            struct.pack(">H", 1) + struct.pack(">B", _TYPE_INTEGER) + struct.pack(">q", 42),
        )

    def test_encode_negative_integer(self):
        """Negative integer encodes as type tag + 8-byte big-endian."""
        encoded = encode_record([-42])
        self.assertEqual(
            encoded,
            struct.pack(">H", 1) + struct.pack(">B", _TYPE_INTEGER) + struct.pack(">q", -42),
        )

    def test_encode_integer_max_boundary(self):
        """Maximum 64-bit signed integer encodes correctly."""
        value = (1 << 63) - 1
        encoded = encode_record([value])
        self.assertEqual(
            encoded,
            struct.pack(">H", 1) + struct.pack(">B", _TYPE_INTEGER) + struct.pack(">q", value),
        )

    def test_encode_integer_min_boundary(self):
        """Minimum 64-bit signed integer encodes correctly."""
        value = -(1 << 63)
        encoded = encode_record([value])
        self.assertEqual(
            encoded,
            struct.pack(">H", 1) + struct.pack(">B", _TYPE_INTEGER) + struct.pack(">q", value),
        )

    def test_encode_empty_string(self):
        """Empty string encodes as type tag + zero length."""
        encoded = encode_record([""])
        self.assertEqual(
            encoded,
            struct.pack(">H", 1) + struct.pack(">B", _TYPE_TEXT) + struct.pack(">I", 0),
        )

    def test_encode_english_string(self):
        """English string encodes as type tag + length + UTF-8 bytes."""
        value = "hello"
        encoded = encode_record([value])
        self.assertEqual(
            encoded,
            struct.pack(">H", 1)
            + struct.pack(">B", _TYPE_TEXT)
            + struct.pack(">I", len(value.encode("utf-8")))
            + value.encode("utf-8"),
        )

    def test_encode_japanese_string(self):
        """Japanese string encodes as type tag + length + UTF-8 bytes."""
        value = "こんにちは"
        encoded = encode_record([value])
        self.assertEqual(
            encoded,
            struct.pack(">H", 1)
            + struct.pack(">B", _TYPE_TEXT)
            + struct.pack(">I", len(value.encode("utf-8")))
            + value.encode("utf-8"),
        )

    def test_encode_multi_column(self):
        """Multiple columns encode in order with column_count."""
        values = [None, 42, "text"]
        encoded = encode_record(values)
        expected = struct.pack(">H", 3)
        expected += struct.pack(">B", _TYPE_NULL)
        expected += struct.pack(">B", _TYPE_INTEGER) + struct.pack(">q", 42)
        expected += struct.pack(">B", _TYPE_TEXT) + struct.pack(">I", 4) + b"text"
        self.assertEqual(encoded, expected)

    def test_encode_column_count(self):
        """Column count is encoded as 2-byte big-endian unsigned short."""
        values = [1, 2, 3]
        encoded = encode_record(values)
        self.assertEqual(encoded[0:2], struct.pack(">H", 3))

    def test_encode_unsupported_type(self):
        """Unsupported value types raise TypeError."""
        with self.assertRaises(TypeError):
            encode_record([object()])

    def test_encode_bool_rejected(self):
        """bool is not a supported record value type."""
        with self.assertRaises(TypeError):
            encode_record([True])

    def test_encode_integer_out_of_range(self):
        """Integers outside 64-bit signed range raise ValueError."""
        with self.assertRaises(ValueError):
            encode_record([1 << 63])

    def test_encode_negative_integer_out_of_range(self):
        """Integers below 64-bit signed range raise ValueError."""
        with self.assertRaises(ValueError):
            encode_record([-(1 << 63) - 1])

    def test_encode_non_list(self):
        """Non-list input raises TypeError."""
        with self.assertRaises(TypeError):
            encode_record(42)


class DecodeRecordTest(unittest.TestCase):
    """Tests for decode_record."""

    def test_decode_none(self):
        """NULL type tag decodes to None."""
        data = struct.pack(">H", 1) + struct.pack(">B", _TYPE_NULL)
        self.assertEqual(decode_record(data), [None])

    def test_decode_positive_integer(self):
        """Positive integer decodes correctly."""
        data = struct.pack(">H", 1) + struct.pack(">B", _TYPE_INTEGER) + struct.pack(">q", 42)
        self.assertEqual(decode_record(data), [42])

    def test_decode_negative_integer(self):
        """Negative integer decodes correctly."""
        data = struct.pack(">H", 1) + struct.pack(">B", _TYPE_INTEGER) + struct.pack(">q", -42)
        self.assertEqual(decode_record(data), [-42])

    def test_decode_integer_max_boundary(self):
        """Maximum 64-bit signed integer decodes correctly."""
        value = (1 << 63) - 1
        data = struct.pack(">H", 1) + struct.pack(">B", _TYPE_INTEGER) + struct.pack(">q", value)
        self.assertEqual(decode_record(data), [value])

    def test_decode_integer_min_boundary(self):
        """Minimum 64-bit signed integer decodes correctly."""
        value = -(1 << 63)
        data = struct.pack(">H", 1) + struct.pack(">B", _TYPE_INTEGER) + struct.pack(">q", value)
        self.assertEqual(decode_record(data), [value])

    def test_decode_empty_string(self):
        """Empty string decodes correctly."""
        data = struct.pack(">H", 1) + struct.pack(">B", _TYPE_TEXT) + struct.pack(">I", 0)
        self.assertEqual(decode_record(data), [""])

    def test_decode_english_string(self):
        """English string decodes correctly."""
        data = struct.pack(">H", 1) + struct.pack(">B", _TYPE_TEXT) + struct.pack(">I", 5) + b"hello"
        self.assertEqual(decode_record(data), ["hello"])

    def test_decode_japanese_string(self):
        """Japanese string decodes correctly."""
        value = "こんにちは"
        data = (
            struct.pack(">H", 1)
            + struct.pack(">B", _TYPE_TEXT)
            + struct.pack(">I", len(value.encode("utf-8")))
            + value.encode("utf-8")
        )
        self.assertEqual(decode_record(data), [value])

    def test_decode_multi_column(self):
        """Multiple columns decode in order."""
        data = struct.pack(">H", 3)
        data += struct.pack(">B", _TYPE_NULL)
        data += struct.pack(">B", _TYPE_INTEGER) + struct.pack(">q", 42)
        data += struct.pack(">B", _TYPE_TEXT) + struct.pack(">I", 4) + b"text"
        self.assertEqual(decode_record(data), [None, 42, "text"])

    def test_decode_invalid_tag(self):
        """Invalid type tag raises CorruptDatabaseError."""
        data = struct.pack(">H", 1) + struct.pack(">B", 99)
        with self.assertRaises(CorruptDatabaseError):
            decode_record(data)

    def test_decode_truncated_count(self):
        """Truncated column count raises CorruptDatabaseError."""
        data = struct.pack(">B", 1)
        with self.assertRaises(CorruptDatabaseError):
            decode_record(data)

    def test_decode_truncated_integer(self):
        """Truncated integer payload raises CorruptDatabaseError."""
        data = struct.pack(">H", 1) + struct.pack(">B", _TYPE_INTEGER) + struct.pack(">q", 42)[:4]
        with self.assertRaises(CorruptDatabaseError):
            decode_record(data)

    def test_decode_truncated_text_length(self):
        """Truncated text length raises CorruptDatabaseError."""
        data = struct.pack(">H", 1) + struct.pack(">B", _TYPE_TEXT) + struct.pack(">I", 5)[:2]
        with self.assertRaises(CorruptDatabaseError):
            decode_record(data)

    def test_decode_truncated_text_bytes(self):
        """Truncated text bytes raises CorruptDatabaseError."""
        data = struct.pack(">H", 1) + struct.pack(">B", _TYPE_TEXT) + struct.pack(">I", 5) + b"hel"
        with self.assertRaises(CorruptDatabaseError):
            decode_record(data)

    def test_decode_invalid_utf8(self):
        """Invalid UTF-8 text raises CorruptDatabaseError."""
        data = struct.pack(">H", 1) + struct.pack(">B", _TYPE_TEXT) + struct.pack(">I", 2) + b"\xff\xfe"
        with self.assertRaises(CorruptDatabaseError):
            decode_record(data)

    def test_decode_trailing_bytes(self):
        """Trailing bytes after record raise CorruptDatabaseError."""
        data = struct.pack(">H", 1) + struct.pack(">B", _TYPE_NULL) + b"\x00"
        with self.assertRaises(CorruptDatabaseError):
            decode_record(data)

    def test_decode_non_bytes(self):
        """Non-bytes input raises TypeError."""
        with self.assertRaises(TypeError):
            decode_record("not bytes")


class RoundtripTest(unittest.TestCase):
    """Roundtrip tests for encode_record/decode_record."""

    def test_roundtrip_none(self):
        """None roundtrips."""
        self.assertEqual(decode_record(encode_record([None])), [None])

    def test_roundtrip_integer(self):
        """Integer roundtrips."""
        self.assertEqual(decode_record(encode_record([42])), [42])

    def test_roundtrip_negative_integer(self):
        """Negative integer roundtrips."""
        self.assertEqual(decode_record(encode_record([-42])), [-42])

    def test_roundtrip_integer_max(self):
        """Maximum 64-bit integer roundtrips."""
        value = (1 << 63) - 1
        self.assertEqual(decode_record(encode_record([value])), [value])

    def test_roundtrip_integer_min(self):
        """Minimum 64-bit integer roundtrips."""
        value = -(1 << 63)
        self.assertEqual(decode_record(encode_record([value])), [value])

    def test_roundtrip_empty_string(self):
        """Empty string roundtrips."""
        self.assertEqual(decode_record(encode_record([""])), [""])

    def test_roundtrip_english_string(self):
        """English string roundtrips."""
        self.assertEqual(decode_record(encode_record(["hello"])), ["hello"])

    def test_roundtrip_japanese_string(self):
        """Japanese string roundtrips."""
        self.assertEqual(decode_record(encode_record(["こんにちは"])), ["こんにちは"])

    def test_roundtrip_multi_column(self):
        """Multi-column record roundtrips preserving order."""
        values = [None, 42, -7, "hello", "こんにちは", ""]
        self.assertEqual(decode_record(encode_record(values)), values)


if __name__ == "__main__":
    unittest.main()