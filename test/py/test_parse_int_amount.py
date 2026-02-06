"""Tests for _parse_int_amount utility function."""

import pytest

from src.models.accounting import _parse_int_amount


class TestParseIntAmount:
    """Tests for the _parse_int_amount function."""

    def test_int_passthrough(self):
        """Test that integers are returned as-is."""
        assert _parse_int_amount(0) == 0
        assert _parse_int_amount(42) == 42
        assert _parse_int_amount(-100) == -100

    def test_large_int_passthrough(self):
        """Test that large integers preserve precision."""
        large_int = 123456789012345678901234567890
        assert _parse_int_amount(large_int) == large_int

    def test_float_truncation(self):
        """Test that floats are truncated to integers."""
        assert _parse_int_amount(3.14) == 3
        assert _parse_int_amount(99.99) == 99
        assert _parse_int_amount(-2.7) == -2

    def test_string_integer(self):
        """Test parsing plain integer strings."""
        assert _parse_int_amount("42") == 42
        assert _parse_int_amount("0") == 0
        assert _parse_int_amount("-100") == -100

    def test_string_with_whitespace(self):
        """Test that whitespace is stripped."""
        assert _parse_int_amount("  42  ") == 42
        assert _parse_int_amount("\n100\t") == 100

    def test_large_string_integer_preserves_precision(self):
        """Test that large integer strings preserve full precision."""
        large_str = "123456789012345678901234567890"
        expected = 123456789012345678901234567890
        assert _parse_int_amount(large_str) == expected

    def test_scientific_notation_string(self):
        """Test parsing scientific notation strings."""
        assert _parse_int_amount("1e6") == 1000000
        assert _parse_int_amount("1E6") == 1000000
        assert _parse_int_amount("2.5e3") == 2500
        assert _parse_int_amount("1.5E2") == 150

    def test_decimal_string(self):
        """Test parsing decimal strings."""
        assert _parse_int_amount("3.14") == 3
        assert _parse_int_amount("99.99") == 99
        assert _parse_int_amount("100.0") == 100

    def test_negative_scientific_notation(self):
        """Test negative scientific notation."""
        assert _parse_int_amount("-1e3") == -1000
        assert _parse_int_amount("1e-1") == 0  # 0.1 truncates to 0

    def test_wei_amounts(self):
        """Test typical wei amounts (18 decimals)."""
        # 1 ETH in wei
        one_eth_wei = "1000000000000000000"
        assert _parse_int_amount(one_eth_wei) == 10**18

        # 100 ETH in wei
        hundred_eth_wei = "100000000000000000000"
        assert _parse_int_amount(hundred_eth_wei) == 100 * 10**18

    def test_scientific_notation_preserves_precision(self):
        """Test that scientific notation preserves full precision."""
        value = "1.123456789012345678e18"
        expected = 1123456789012345678
        assert _parse_int_amount(value) == expected

    def test_scientific_notation_1_5_eth(self):
        """Test 1.5 ETH expressed as scientific notation."""
        assert _parse_int_amount("1.5e18") == 1500000000000000000

    def test_invalid_string_raises(self):
        """Test that invalid strings raise an exception."""
        from decimal import InvalidOperation

        with pytest.raises(InvalidOperation):
            _parse_int_amount("not a number")
        with pytest.raises(ValueError):
            _parse_int_amount("")
        with pytest.raises(InvalidOperation):
            _parse_int_amount("12.34.56")
