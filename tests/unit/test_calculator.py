"""Unit tests for calculation engine, tare mathematics, mass conservation, micro-costing, and zero-weight detection."""

import decimal
import random
import sys
import unittest
from decimal import Decimal
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[2] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from tared.core.calculator import (
    IncompatibleTargetPrecisionError,
    PercentageSumError,
    compute_batch,
    validate_recipe_percentages,
)
from tared.core.models import Ingredient, Recipe
from tests.conftest import create_test_recipe


class TestCalculatorValidation(unittest.TestCase):
    def test_core_package_zero_qt_leakage(self) -> None:
        import subprocess

        code = (
            "import sys\n"
            f"sys.path.insert(0, {str(SRC_DIR)!r})\n"
            "sys.modules['PyQt6'] = None\n"
            "sys.modules['PyQt6.QtCore'] = None\n"
            "sys.modules['PyQt6.QtWidgets'] = None\n"
            "import tared.core\n"
            "from tared.core.calculator import compute_batch\n"
            "from tared.core.formatting import format_currency\n"
            "from tared.core.models import Recipe, Ingredient\n"
        )
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"tared.core failed headless import:\n{res.stderr}")

    def test_invalid_percentage_sum_raises_error(self) -> None:
        bad_recipe = Recipe(
            name="Imbalanced Formula",
            ingredients=(
                Ingredient(name="Component A", percentage=Decimal("50.00")),
                Ingredient(name="Component B", percentage=Decimal("49.00")),
            ),
        )
        with self.assertRaises(PercentageSumError):
            validate_recipe_percentages(bad_recipe)

    def test_negative_target_weight_raises_error(self) -> None:
        recipe = Recipe(
            name="Valid",
            ingredients=(Ingredient(name="Base", percentage=Decimal("100.00")),),
        )
        with self.assertRaises(ValueError):
            compute_batch(recipe, target_weight=Decimal("-10.00"))

    def test_float_input_rejected_with_type_error(self) -> None:
        recipe = Recipe(
            name="Valid",
            ingredients=(Ingredient(name="Base", percentage=Decimal("100.00")),),
        )
        with self.assertRaises(TypeError):
            compute_batch(recipe, target_weight=100.5)
        with self.assertRaises(TypeError):
            compute_batch(recipe, target_weight=Decimal("100.00"), precision=0.01)

    def test_incompatible_target_precision_raises_error(self) -> None:
        recipe = Recipe(
            name="Valid",
            ingredients=(Ingredient(name="Base", percentage=Decimal("100.00")),),
        )
        with self.assertRaises(IncompatibleTargetPrecisionError):
            compute_batch(recipe, target_weight=Decimal("1.054"), precision=Decimal("0.01"))

        batch = compute_batch(recipe, target_weight=Decimal("1.05"), precision=Decimal("0.01"))
        self.assertEqual(batch.total_weight, Decimal("1.05"))

    def test_non_finite_inputs_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Ingredient(name="Invalid NaN", percentage=Decimal("NaN"))

        with self.assertRaises(ValueError):
            Ingredient(name="Invalid Inf", percentage=Decimal("Infinity"))

        recipe = Recipe(
            name="Finite",
            ingredients=(Ingredient(name="Base", percentage=Decimal("100.00")),),
        )
        with self.assertRaises(ValueError):
            compute_batch(recipe, target_weight=Decimal("NaN"))


class TestBatchScalingAndConservation(unittest.TestCase):
    def setUp(self) -> None:
        self.recipe = create_test_recipe()

    def test_conservation_of_mass_invariant(self) -> None:
        test_weights = [
            Decimal("100.00"),
            Decimal("500.00"),
            Decimal("10000.00"),
            Decimal("333.33"),
            Decimal("1.05"),
        ]
        for target_weight in test_weights:
            with self.subTest(target_weight=target_weight):
                batch = compute_batch(self.recipe, target_weight=target_weight, precision=Decimal("0.01"))
                calculated_total = sum(item.scaled_weight for item in batch.items)
                self.assertEqual(calculated_total, target_weight)
                self.assertEqual(batch.total_weight, target_weight)

    def test_cumulative_tare_integrity(self) -> None:
        target = Decimal("1000.00")
        batch = compute_batch(self.recipe, target_weight=target)

        running_sum = Decimal("0")
        for item in batch.items:
            running_sum += item.scaled_weight
            self.assertEqual(item.cumulative_tare, running_sum)

        self.assertEqual(batch.items[-1].cumulative_tare, target)

    def test_micro_costs_preserved_without_premature_truncation(self) -> None:
        ings = tuple(
            Ingredient(name=f"Ing_{i}", percentage=Decimal("5.00"), cost_per_unit=Decimal("0.40")) for i in range(20)
        )
        recipe = Recipe(name="Micro Cost Formula", ingredients=ings)
        batch = compute_batch(recipe, target_weight=Decimal("100.00"), precision=Decimal("0.01"))

        for item in batch.items:
            self.assertEqual(item.cost, Decimal("0.0020"))

        self.assertEqual(batch.total_cost, Decimal("0.0400"))

    def test_currency_quantization_and_unrounded_total_accumulation(self) -> None:
        # 3 items with unrounded cost of $0.004 each (40g at $0.10/kg = $0.004)
        # Sum of individually rounded 2-decimal lines: $0.00 + $0.00 + $0.00 = $0.00
        # Sum first, then round: $0.012 -> $0.01
        ings = (
            Ingredient(name="Item 1", percentage=Decimal("33.34"), cost_per_unit=Decimal("0.10")),
            Ingredient(name="Item 2", percentage=Decimal("33.33"), cost_per_unit=Decimal("0.10")),
            Ingredient(name="Item 3", percentage=Decimal("33.33"), cost_per_unit=Decimal("0.10")),
        )
        recipe = Recipe(name="Subcent Total Test", ingredients=ings)
        batch = compute_batch(
            recipe,
            target_weight=Decimal("120.00"),
            precision=Decimal("0.01"),
            currency_precision=2,
        )

        # Lines round to nearest whole cent ($0.00)
        for item in batch.items:
            self.assertEqual(item.cost, Decimal("0.00"))

        # Batch total is rounded from the unrounded total ($0.012 -> $0.01)
        self.assertEqual(batch.total_cost, Decimal("0.01"))

    def test_currency_precision_step_increments(self) -> None:
        ings = (Ingredient(name="Base", percentage=Decimal("100.00"), cost_per_unit=Decimal("1.30")),)
        recipe = Recipe(name="Nickel Rounding Test", ingredients=ings)
        # 100g at $1.30/kg = $0.13 raw cost
        # Rounded to nearest $0.05 step -> $0.15
        batch = compute_batch(
            recipe,
            target_weight=Decimal("100.00"),
            currency_precision=Decimal("0.05"),
        )
        self.assertEqual(batch.items[0].cost, Decimal("0.15"))
        self.assertEqual(batch.total_cost, Decimal("0.15"))

    def test_ambient_decimal_context_isolation(self) -> None:
        original_context = decimal.getcontext().copy()
        try:
            # Set restrictive ambient context to prove compute_batch remains isolated
            decimal.getcontext().prec = 4
            decimal.getcontext().rounding = decimal.ROUND_DOWN

            batch = compute_batch(self.recipe, target_weight=Decimal("333.33"), precision=Decimal("0.01"))
        finally:
            decimal.setcontext(original_context)

        self.assertEqual(batch.total_weight, Decimal("333.33"))
        self.assertEqual(sum(it.scaled_weight for it in batch.items), Decimal("333.33"))

    def test_deterministic_allocation_tie_breaking(self) -> None:
        # Case 1: Equal remainders, unequal percentages -> higher percentage gets extra unit
        # 10g target at 1g precision:
        # Component A (60%): 6.0g
        # Component B (25%): 2.5g -> floor 2g, rem 0.5g
        # Component C (15%): 1.5g -> floor 1g, rem 0.5g
        # Deficit is 1g (10 - 9). Both B and C have 0.5g remainder. B has 25% > 15%, so B gets the unit.
        recipe1 = Recipe(
            name="Tie Break Percentage Test",
            ingredients=(
                Ingredient(name="A", percentage=Decimal("60.00")),
                Ingredient(name="B", percentage=Decimal("25.00")),
                Ingredient(name="C", percentage=Decimal("15.00")),
            ),
        )
        batch1 = compute_batch(recipe1, target_weight=Decimal("10"), precision=Decimal("1"))
        self.assertEqual(batch1.items[0].scaled_weight, Decimal("6"))
        self.assertEqual(batch1.items[1].scaled_weight, Decimal("3"))
        self.assertEqual(batch1.items[2].scaled_weight, Decimal("1"))

        # Case 2: Equal remainders, equal percentages -> lower index (first in formula) gets extra unit
        # 10g target at 1g precision:
        # Component X (50%): 5.0g
        # Component Y (25%): 2.5g -> floor 2g, rem 0.5g
        # Component Z (25%): 2.5g -> floor 2g, rem 0.5g
        # Deficit is 1g (10 - 9). Equal remainder and equal percentage. Y is index 1, Z is index 2 -> Y gets the unit.
        recipe2 = Recipe(
            name="Tie Break Sequence Test",
            ingredients=(
                Ingredient(name="X", percentage=Decimal("50.00")),
                Ingredient(name="Y", percentage=Decimal("25.00")),
                Ingredient(name="Z", percentage=Decimal("25.00")),
            ),
        )
        batch2 = compute_batch(recipe2, target_weight=Decimal("10"), precision=Decimal("1"))
        self.assertEqual(batch2.items[0].scaled_weight, Decimal("5"))
        self.assertEqual(batch2.items[1].scaled_weight, Decimal("3"))
        self.assertEqual(batch2.items[2].scaled_weight, Decimal("2"))

    def test_relative_allocation_deviation_detection(self) -> None:
        recipe = Recipe(
            name="Deviation Recipe",
            ingredients=(
                Ingredient(name="Active Extract", percentage=Decimal("1.50")),
                Ingredient(name="Water", percentage=Decimal("98.50")),
            ),
        )
        # Raw weights for 100g: Active = 1.50g, Water = 98.50g
        # At 1g precision: Active rounds to 1g or 2g. Either way it shifts by 0.5g.
        # 0.5g / 1.50g = 33.3% relative deviation (>25% threshold).
        batch = compute_batch(recipe, target_weight=Decimal("100"), precision=Decimal("1"))

        self.assertIn("Active Extract", batch.high_deviation_ingredients)
        self.assertNotIn("Water", batch.high_deviation_ingredients)

    def test_trace_ingredient_zero_allocation_detected(self) -> None:
        recipe = Recipe(
            name="Trace Component Recipe",
            ingredients=(
                Ingredient(name="Micro Trace Active", percentage=Decimal("0.001")),
                Ingredient(name="Bulk Excipient", percentage=Decimal("99.999")),
            ),
        )
        batch = compute_batch(recipe, target_weight=Decimal("10.00"), precision=Decimal("0.01"))
        self.assertIn("Micro Trace Active", batch.zero_weight_ingredients)
        self.assertEqual(batch.items[0].scaled_weight, Decimal("0.00"))

    def test_randomized_mass_conservation_property(self) -> None:
        rnd = random.Random(42)
        precisions = [Decimal("0.001"), Decimal("0.01"), Decimal("0.1"), Decimal("1.0")]

        for _ in range(50):
            num_components = rnd.randint(2, 10)
            raw_splits = [rnd.randint(1, 1000) for _ in range(num_components)]
            total_raw = sum(raw_splits)

            percentages = [
                (Decimal(split) * Decimal("100") / Decimal(total_raw)).quantize(Decimal("0.0001"))
                for split in raw_splits
            ]
            diff = Decimal("100.0000") - sum(percentages)
            percentages[0] += diff

            ingredients = tuple(Ingredient(name=f"Comp_{i}", percentage=pct) for i, pct in enumerate(percentages))
            recipe = Recipe(name="Random Formulation", ingredients=ingredients)

            precision = rnd.choice(precisions)
            target_units = rnd.randint(10, 100000)
            target = Decimal(target_units) * precision

            batch = compute_batch(recipe, target_weight=target, precision=precision)
            self.assertEqual(batch.total_weight, target)
            self.assertEqual(sum(item.scaled_weight for item in batch.items), target)
            self.assertEqual(batch.items[-1].cumulative_tare, target)

    def test_max_allocation_deviation_strictly_less_than_precision(self) -> None:
        batch = compute_batch(self.recipe, target_weight=Decimal("333.33"), precision=Decimal("0.01"))
        self.assertGreater(batch.max_allocation_deviation, Decimal("0"))
        self.assertLess(batch.max_allocation_deviation, batch.precision)


if __name__ == "__main__":
    unittest.main()
