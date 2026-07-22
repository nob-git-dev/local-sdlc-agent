"""
Record Codec Tests

Tests for the record encoding and decoding functionality.
"""

import unittest

from minisqlite.errors import CorruptDatabaseError, TypeMismatchError
from minisqlite.storage.record import RecordCodec, TYPE_NULL, TYPE_INTEGER, TYPE_TEXT


class TestIntegerEncoding(unittest.TestCase):
    """Test INTEGER encoding and decoding."""

    def test_positive_integer(self):
        """Test encoding and decoding a positive integer."""
        values = [42]
        payload = RecordCodec.encode(values)
        decoded = RecordCodec.decode(payload)
        self.assertEqual(decoded, [42])

    def test_negative_integer(self):
        """Test encoding and decoding a negative integer."""
        values = [-10]
        payload = RecordCodec.encode(values)
        decoded = RecordCodec.decode(payload)
        self.assertEqual(decoded, [-10])

    def test_zero(self):
        """Test encoding and decoding zero."""
        values = [0]
        payload = RecordCodec.encode(values)
        decoded = RecordCodec.decode(payload)
        self.assertEqual(decoded, [0])

    def test_large_integer(self):
        """Test encoding and decoding a large 64-bit integer."""
        values = [9223372036854775807]  # Max int64
        payload = RecordCodec.encode(values)
        decoded = RecordCodec.decode(payload)
        self.assertEqual(decoded, [9223372036854775807])

    def test_small_negative_integer(self):
        """Test encoding and decoding a small negative integer."""
        values = [-9223372036854775808]  # Min int64
        payload = RecordCodec.encode(values)
        decoded = RecordCodec.decode(payload)
        self.assertEqual(decoded, [-9223372036854775808])


class TestTextEncoding(unittest.TestCase):
    """Test TEXT encoding and decoding."""

    def test_simple_string(self):
        """Test encoding and decoding a simple ASCII string."""
        values = ["hello"]
        payload = RecordCodec.encode(values)
        decoded = RecordCodec.decode(payload)
        self.assertEqual(decoded, ["hello"])

    def test_empty_string(self):
        """Test encoding and decoding an empty string."""
        values = [""]
        payload = RecordCodec.encode(values)
        decoded = RecordCodec.decode(payload)
        self.assertEqual(decoded, [""])

    def test_string_with_spaces(self):
        """Test encoding and decoding a string with spaces."""
        values = ["hello world"]
        payload = RecordCodec.encode(values)
        decoded = RecordCodec.decode(payload)
        self.assertEqual(decoded, ["hello world"])

    def test_escaped_quote_in_string(self):
        """Test encoding and decoding a string with quotes."""
        values = ["It's OK"]
        payload = RecordCodec.encode(values)
        decoded = RecordCodec.decode(payload)
        self.assertEqual(decoded, ["It's OK"])

    def test_unicode_string(self):
        """Test encoding and decoding a UTF-8 string."""
        values = ["こんにちは"]
        payload = RecordCodec.encode(values)
        decoded = RecordCodec.decode(payload)
        self.assertEqual(decoded, ["こんにちは"])

    def test_mixed_unicode_and_ascii(self):
        """Test encoding and decoding a mixed ASCII and UTF-8 string."""
        values = ["Hello 世界"]
        payload = RecordCodec.encode(values)
        decoded = RecordCodec.decode(payload)
        self.assertEqual(decoded, ["Hello 世界"])


class TestMultiColumnEncoding(unittest.TestCase):
    """Test multi-column record encoding and decoding."""

    def test_two_columns(self):
        """Test encoding and decoding two columns."""
        values = [1, "Alice"]
        payload = RecordCodec.encode(values)
        decoded = RecordCodec.decode(payload)
        self.assertEqual(decoded, [1, "Alice"])

    def test_three_columns(self):
        """Test encoding and decoding three columns."""
        values = [1, "Alice", 30]
        payload = RecordCodec.encode(values)
        decoded = RecordCodec.decode(payload)
        self.assertEqual(decoded, [1, "Alice", 30])

    def test_all_integer_columns(self):
        """Test encoding and decoding all INTEGER columns."""
        values = [1, 2, 3]
        payload = RecordCodec.encode(values)
        decoded = RecordCodec.decode(payload)
        self.assertEqual(decoded, [1, 2, 3])

    def test_all_text_columns(self):
        """Test encoding and decoding all TEXT columns."""
        values = ["a", "b", "c"]
        payload = RecordCodec.encode(values)
        decoded = RecordCodec.decode(payload)
        self.assertEqual(decoded, ["a", "b", "c"])

    def test_mixed_types(self):
        """Test encoding and decoding mixed INTEGER and TEXT columns."""
        values = [1, "Alice", 30, "Active"]
        payload = RecordCodec.encode(values)
        decoded = RecordCodec.decode(payload)
        self.assertEqual(decoded, [1, "Alice", 30, "Active"])


class TestNullEncoding(unittest.TestCase):
    """Test NULL value encoding and decoding."""

    def test_null_value(self):
        """Test encoding and decoding a NULL value."""
        values = [None]
        payload = RecordCodec.encode(values)
        decoded = RecordCodec.decode(payload)
        self.assertEqual(decoded, [None])

    def test_mixed_null_and_values(self):
        """Test encoding and decoding mixed NULL and non-NULL values."""
        values = [1, None, "Alice"]
        payload = RecordCodec.encode(values)
        decoded = RecordCodec.decode(payload)
        self.assertEqual(decoded, [1, None, "Alice"])


class TestCorruptionDetection(unittest.TestCase):
    """Test detection of corrupted payloads."""

    def test_payload_too_short(self):
        """Test that a payload shorter than 2 bytes raises an error."""
        with self.assertRaises(CorruptDatabaseError):
            RecordCodec.decode(b"\x00")

    def test_empty_payload(self):
        """Test that an empty payload raises an error."""
        with self.assertRaises(CorruptDatabaseError):
            RecordCodec.decode(b"")

    def test_truncated_integer(self):
        """Test that a truncated INTEGER value raises an error."""
        # Create a payload with column count = 1, type tag = INTEGER, but no data
        payload = b"\x00\x01\x01"  # 1 column, INTEGER tag, but no 8 bytes
        with self.assertRaises(CorruptDatabaseError):
            RecordCodec.decode(payload)

    def test_truncated_text_length(self):
        """Test that a truncated TEXT length raises an error."""
        # Create a payload with column count = 1, type tag = TEXT, but no length
        payload = b"\x00\x01\x02"  # 1 column, TEXT tag, but no 4 bytes for length
        with self.assertRaises(CorruptDatabaseError):
            RecordCodec.decode(payload)

    def test_truncated_text_data(self):
        """Test that a truncated TEXT data raises an error."""
        # Create a payload with column count = 1, type tag = TEXT, length = 10, but no data
        payload = b"\x00\x01\x02\x00\x00\x00\x0a"  # 1 column, TEXT tag, length=10, no data
        with self.assertRaises(CorruptDatabaseError):
            RecordCodec.decode(payload)

    def test_invalid_type_tag(self):
        """Test that an invalid type tag raises an error."""
        # Create a payload with an invalid type tag (e.g., 3)
        payload = b"\x00\x01\x03"  # 1 column, invalid type tag
        with self.assertRaises(CorruptDatabaseError):
            RecordCodec.decode(payload)

    def test_invalid_utf8(self):
        """Test that invalid UTF-8 in TEXT raises an error."""
        # Create a payload with invalid UTF-8 bytes
        payload = b"\x00\x01\x02\x00\x00\x00\x03\xff\xfe\xfd"  # 1 column, TEXT, length=3, invalid UTF-8
        with self.assertRaises(CorruptDatabaseError):
            RecordCodec.decode(payload)


class TestTypeValidation(unittest.TestCase):
    """Test type validation for records."""

    def test_valid_integer(self):
        """Test that a valid INTEGER passes validation."""
        values = [42]
        expected_types = ["INTEGER"]
        RecordCodec.validate_types(values, expected_types)  # Should not raise

    def test_valid_text(self):
        """Test that a valid TEXT passes validation."""
        values = ["hello"]
        expected_types = ["TEXT"]
        RecordCodec.validate_types(values, expected_types)  # Should not raise

    def test_integer_instead_of_text(self):
        """Test that an INTEGER instead of TEXT raises an error."""
        values = [42]
        expected_types = ["TEXT"]
        with self.assertRaises(TypeMismatchError):
            RecordCodec.validate_types(values, expected_types)

    def test_text_instead_of_integer(self):
        """Test that a TEXT instead of INTEGER raises an error."""
        values = ["hello"]
        expected_types = ["INTEGER"]
        with self.assertRaises(TypeMismatchError):
            RecordCodec.validate_types(values, expected_types)

    def test_column_count_mismatch(self):
        """Test that a column count mismatch raises an error."""
        values = [1, 2]
        expected_types = ["INTEGER"]
        with self.assertRaises(TypeMismatchError):
            RecordCodec.validate_types(values, expected_types)

    def test_null_with_type(self):
        """Test that NULL is allowed regardless of expected type."""
        values = [None]
        expected_types = ["INTEGER"]
        RecordCodec.validate_types(values, expected_types)  # Should not raise

    def test_multiple_columns_valid(self):
        """Test that multiple valid columns pass validation."""
        values = [1, "Alice", 30]
        expected_types = ["INTEGER", "TEXT", "INTEGER"]
        RecordCodec.validate_types(values, expected_types)  # Should not raise

    def test_multiple_columns_invalid(self):
        """Test that multiple columns with type mismatch raises an error."""
        # Test case: values=[1, 30, "Alice"] with expected_types=['INTEGER', 'INTEGER', 'TEXT']
        # This is actually VALID input (1=INTEGER, 30=INTEGER, "Alice"=TEXT)
        # The test name is misleading - we need a truly invalid case
        # Using values=[1, "wrong", "Alice"] where second value is TEXT but expected INTEGER
        values = [1, "wrong", "Alice"]
        expected_types = ["INTEGER", "INTEGER", "TEXT"]
        with self.assertRaises(TypeMismatchError):
            RecordCodec.validate_types(values, expected_types)

    def test_bool_rejected_as_integer(self):
        """Test that bool is explicitly rejected for INTEGER columns."""
        values = [True]
        expected_types = ["INTEGER"]
        with self.assertRaises(TypeMismatchError):
            RecordCodec.validate_types(values, expected_types)

    def test_empty_values_with_types(self):
        """Test that empty values list with non-empty types raises error."""
        values = []
        expected_types = ["INTEGER"]
        with self.assertRaises(TypeMismatchError):
            RecordCodec.validate_types(values, expected_types)

    def test_strict_validation_empty_both(self):
        """Test that strict validation passes when both lists are empty."""
        values = []
        expected_types = []
        RecordCodec.validate_types_strict(values, expected_types)  # Should not raise

    def test_strict_validation_mismatch(self):
        """Test that strict validation fails on count mismatch."""
        values = [1]
        expected_types = ["INTEGER", "TEXT"]
        with self.assertRaises(TypeMismatchError):
            RecordCodec.validate_types_strict(values, expected_types)