"""Unit tests verifying unified percentage and currency formatting across grid, summary, and print."""

import unittest
from decimal import Decimal

from tared.core.formatting import (
    determine_cost_precision,
    format_currency,
    format_percentage,
)


class TestFormatting(unittest.TestCase):
    def test_format_percentage_preserves_two_decimals_minimum(self) -> None:
        self.assertEqual(format_percentage(Decimal("33")), "33.00")
        self.assertEqual(format_percentage(Decimal("33.0")), "33.00")
        self.assertEqual(format_percentage(Decimal("12.5")), "12.50")
        self.assertEqual(format_percentage(Decimal("100.0000")), "100.00")

    def test_format_percentage_expands_for_micro_traces(self) -> None:
        self.assertEqual(format_percentage(Decimal("0.0001")), "0.0001")
        self.assertEqual(format_percentage(Decimal("99.9999")), "99.9999")
        self.assertEqual(format_percentage(Decimal("0.000001")), "0.000001")

    def test_format_currency_standard_amounts(self) -> None:
        self.assertEqual(format_currency(Decimal("0")), "$0.00")
        self.assertEqual(format_currency(Decimal("5")), "$5.00")
        self.assertEqual(format_currency(Decimal("20.00")), "$20.00")
        self.assertEqual(format_currency(Decimal("12.5")), "$12.50")
        self.assertEqual(format_currency(Decimal("0.05")), "$0.05")

    def test_format_currency_half_cent_tie_rounding(self) -> None:
        self.assertEqual(format_currency(Decimal("0.125"), precision=2), "$0.13")
        self.assertEqual(format_currency(Decimal("0.12500"), precision=2), "$0.13")
        self.assertEqual(format_currency(Decimal("0.12499"), precision=2), "$0.12")

    def test_format_currency_fractional_cents_and_micro_costs(self) -> None:
        self.assertEqual(format_currency(Decimal("0.125")), "$0.1250")
        self.assertEqual(format_currency(Decimal("0.002")), "$0.0020")
        self.assertEqual(format_currency(Decimal("0.0001")), "$0.0001")
        self.assertEqual(format_currency(Decimal("0.000001")), "$0.000001")

    def test_format_currency_sub_nano_and_negative_amounts(self) -> None:
        self.assertEqual(format_currency(Decimal("1e-9")), "$0.000000001")
        self.assertEqual(format_currency(Decimal("-12.50")), "-$12.50")
        self.assertEqual(format_currency(Decimal("-0.002")), "-$0.0020")

    def test_format_currency_precision_given_with_underflow_expands(self) -> None:
        result = format_currency(Decimal("0.000001"), precision=2)
        self.assertEqual(result, "$0.000001")
        self.assertEqual(format_currency(Decimal("1.256"), precision=2), "$1.26")
        self.assertEqual(format_currency(Decimal("1.2"), precision=2), "$1.20")

    def test_format_currency_unrounded_value_capped_to_reasonable_precision(self) -> None:
        unrounded_third = Decimal("1") / Decimal("3")
        formatted = format_currency(unrounded_third)
        # Bounded within 10 decimal digits maximum
        self.assertLessEqual(len(formatted.split(".")[1]), 10)

    def test_determine_cost_precision_detection(self) -> None:
        standard_costs = [Decimal("1.50"), Decimal("2.00"), Decimal("0.25")]
        self.assertEqual(determine_cost_precision(standard_costs), 2)

        subcent_costs = [Decimal("1.50"), Decimal("0.002"), Decimal("0.25")]
        self.assertEqual(determine_cost_precision(subcent_costs), 4)

        fractional_cent_costs = [Decimal("1.50"), Decimal("0.125"), Decimal("0.25")]
        self.assertEqual(determine_cost_precision(fractional_cent_costs), 4)


if __name__ == "__main__":
    unittest.main()
