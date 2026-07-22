"""Tests for minisqlite.storage.record."""

import struct
import unittest

from minisqlite.errors import CorruptDatabaseError
from minisqlite.storage.record import (
    decode_record,
    encode_record,
)


class TestEncodeDecodeRoundTrip(unittest.TestCase):
    """Round-trip tests: encode then decode should return the original values."""

    def test_integer_round_trip(self):
        values = [42, -100, 0, 2**63 - 1, -(2**63)]
        encoded = encode_record(values)
        decoded = decode_record(encoded)
        self.assertEqual(decoded, values)

    def test_text_round_trip(self):
        values = ["hello", "", "world"]
        encoded = encode_record(values)
        decoded = decode_record(encoded)
        self.assertEqual(decoded, values)

    def test_multiple_columns_round_trip(self):
        values = [42, "hello", None, -7, ""]
        encoded = encode_record(values)
        decoded = decode_record(encoded)
        self.assertEqual(decoded, values)

    def test_utf8_japanese_round_trip(self):
        values = ["こんにちは", "日本語テスト", "🎉"]
        encoded = encode_record(values)
        decoded = decode_record(encoded)
        self.assertEqual(decoded, values)

    def test_none_round_trip(self):
        values = [None, 1, "a", None]
        encoded = encode_record(values)
        decoded = decode_record(encoded)
        self.assertEqual(decoded, values)

    def test_empty_record(self):
        values = []
        encoded = encode_record(values)
        decoded = decode_record(encoded)
        self.assertEqual(decoded, values)


class TestDecodeCorruption(unittest.TestCase):
    """Truncated or malformed payloads must raise CorruptDatabaseError."""

    def _make_record(self, column_count: int, type_bytes: list[bytes]) -> bytes:
        """Helper to build a partial record for truncation tests."""
        header = struct.pack(">H", column_count)
        return header + b"".join(type_bytes)

    def test_truncated_column_count(self):
        with self.assertRaises(CorruptDatabaseError):
            decode_record(b"")

    def test_truncated_column_count_one_byte(self):
        with self.assertRaises(CorruptDatabaseError):
            decode_record(b"\x00")

    def test_truncated_integer(self):
        # 1 column, INTEGER type, but only 4 of 8 bytes follow
        payload = struct.pack(">H", 1) + b"\x01" + struct.pack(">q", 42)[:4]
        with self.assertRaises(CorruptDatabaseError):
            decode_record(payload)

    def test_truncated_text_length(self):
        # 1 column, TEXT type, but only 2 of 4 length bytes follow
        payload = struct.pack(">H", 1) + b"\x02" + b"\x00\x01"
        with self.assertRaises(CorruptDatabaseError):
            decode_record(payload)

    def test_truncated_text_payload(self):
        # 1 column, TEXT type, length says 10 bytes but only 3 provided
        text = b"abc"
        payload = (
            struct.pack(">H", 1)
            + b"\x02"
            + struct.pack(">I", 10)
            + text
        )
        with self.assertRaises(CorruptDatabaseError):
            decode_record(payload)

    def test_unknown_type_tag(self):
        # 1 column, type tag 0xFF which is unknown
        payload = struct.pack(">H", 1) + b"\xFF"
        with self.assertRaises(CorruptDatabaseError):
            decode_record(payload)


class TestEncodeErrors(unittest.TestCase):
    """encode_record should reject unsupported types."""

    def test_unsupported_type_float(self):
        with self.assertRaises(TypeError):
            encode_record([3.14])

    def test_unsupported_type_bytes(self):
        with self.assertRaises(TypeError):
            encode_record([b"binary"])


if __name__ == "__main__":
    unittest.main()
