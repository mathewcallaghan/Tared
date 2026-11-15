"""Property-based stress and boundary fuzzing tests for core calculation, parser, and workspace integrity."""

import json
import random
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from tared.core.calculator import compute_batch
from tared.core.models import Ingredient, Recipe
from tared.storage.json_repo import recipe_from_dict
from tared.storage.sqlite_repo import SQLiteRecipeRepository
from tared.storage.workspace_io import export_workspace_to_dict, inspect_workspace_file
from tared.ui.models.recipe_table_model import _parse_decimal_input


class TestPropertyFuzzing(unittest.TestCase):
    def test_fuzz_hare_niemeyer_mass_conservation(self) -> None:
        """Fuzz compute_batch across 500 randomized formulations, precisions, and target masses."""
        rnd = random.Random(42)
        precisions = [Decimal("0.001"), Decimal("0.01"), Decimal("0.1"), Decimal("1")]

        for iteration in range(500):
            num_ingredients = rnd.randint(1, 40)
            raw_weights = [rnd.randint(1, 10000) for _ in range(num_ingredients)]
            sum_raw = sum(raw_weights)

            percentages = [
                (Decimal(w) * Decimal("100") / Decimal(sum_raw)).quantize(Decimal("0.0001"))
                for w in raw_weights
            ]
            deficit = Decimal("100.0000") - sum(percentages)
            percentages[0] += deficit

            ingredients = tuple(
                Ingredient(name=f"Comp_{i}", percentage=pct, cost_per_unit=Decimal("1.50"))
                for i, pct in enumerate(percentages)
            )
            recipe = Recipe(name=f"Fuzz_Recipe_{iteration}", ingredients=ingredients)

            precision = rnd.choice(precisions)
            target_units = rnd.randint(1, 50000)
            target_mass = Decimal(target_units) * precision

            batch = compute_batch(recipe, target_weight=target_mass, precision=precision)

            # Invariant 1: Sum of discrete scaled weights exactly equals target mass
            total_scaled = sum(it.scaled_weight for it in batch.items)
            self.assertEqual(total_scaled, target_mass)
            self.assertEqual(batch.total_weight, target_mass)

            # Invariant 2: Running cumulative tare monotonicity and endpoint match
            running_sum = Decimal("0")
            for it in batch.items:
                running_sum += it.scaled_weight
                self.assertEqual(it.cumulative_tare, running_sum)
                # Invariant 3: Every scaled component is an exact multiple of scale precision
                quot = it.scaled_weight / precision
                self.assertEqual(quot, quot.to_integral_value())

            self.assertEqual(batch.items[-1].cumulative_tare, target_mass)

    def test_fuzz_parse_decimal_input_robustness(self) -> None:
        """Fuzz numeric parser with randomized string mutations; must only raise ValueError on invalid input."""
        rnd = random.Random(42)
        valid_seeds = ["100", "0.01", "1234.56", "0.0001", "50g", "1.5kg", "25%", "$12.50", "1e-4"]
        corrupt_chars = [
            "", " ", "\t", "\xa0", "\u202f", "e", "E", "-", "+", ".", ",", "%", "$",
            "g", "kg", "x", "\x00",
        ]

        for _ in range(1000):
            base = rnd.choice(valid_seeds)
            # Apply random insert, delete, or replacement
            mutation_type = rnd.choice(["insert", "delete", "replace", "suffix", "prefix"])
            pos = rnd.randint(0, len(base))
            char = rnd.choice(corrupt_chars)

            if mutation_type == "insert":
                fuzzed = base[:pos] + char + base[pos:]
            elif mutation_type == "delete" and len(base) > 1:
                fuzzed = base[:pos] + base[pos + 1 :]
            elif mutation_type == "replace" and len(base) > 0:
                fuzzed = base[:pos] + char + base[min(pos + 1, len(base)) :]
            elif mutation_type == "prefix":
                fuzzed = char + base
            else:
                fuzzed = base + char

            try:
                dec = _parse_decimal_input(fuzzed, convert_kg=True, allow_mass_units=True)
                self.assertTrue(dec.is_finite())
            except ValueError:
                pass  # Graceful rejection

    def test_fuzz_workspace_inspection_integrity_rejection(self) -> None:
        """Fuzz inspect_workspace_file with structural and type corruptions."""
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(
            Recipe(
                id="fuzz-base",
                name="Fuzz Base",
                ingredients=(
                    Ingredient("Phase A", Decimal("50.00")),
                    Ingredient("Phase B", Decimal("50.00")),
                ),
            )
        )
        repo.record_batch("fuzz-base", 1, "BAT-FUZZ-01", "Op", "100.00", "100.00", "1.00")
        valid_snapshot = export_workspace_to_dict(repo)
        corruption_keys = [
            ("schema_version", "99.0"),
            ("catalog", "not-a-list"),
            ("recipes", None),
            ("revisions", {}),
            ("batches", 123),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "mutated_ws.json"

            # 1. Test top-level schema corruptions
            for key, bad_val in corruption_keys:
                mutated = dict(valid_snapshot)
                mutated[key] = bad_val
                tmp_path.write_text(json.dumps(mutated), encoding="utf-8")
                with self.assertRaises(ValueError):
                    inspect_workspace_file(tmp_path)

            # 2. Test recipe ingredient corruptions (NaN, missing, wrong types)
            for bad_ing in [
                {"name": "", "percentage": "100.00"},
                {"name": "A", "percentage": "-10.00"},
                {"name": "A", "percentage": "NaN"},
                {"name": "A"},
                None,
                "not-a-dict",
            ]:
                mutated = json.loads(json.dumps(valid_snapshot))
                mutated["recipes"][0]["ingredients"] = [bad_ing]
                tmp_path.write_text(json.dumps(mutated), encoding="utf-8")
                with self.assertRaises(ValueError):
                    inspect_workspace_file(tmp_path)

    def test_fuzz_recipe_from_dict_boundary_rejection(self) -> None:
        """Verify recipe_from_dict rejects invalid JSON objects without unhandled exceptions."""
        invalid_payloads = [
            None,
            [],
            "string",
            {},
            {"version": "2.0"},
            {"name": ""},
            {"name": "Valid", "ingredients": []},
            {"name": "Valid", "ingredients": [{"name": "A", "percentage": "0"}]},
            {"name": "Valid", "ingredients": [{"name": "A", "percentage": "Infinity"}]},
            {"name": "Valid", "recipe_version": "not-an-int", "ingredients": [{"name": "A", "percentage": "100"}]},
        ]
        for bad in invalid_payloads:
            with self.assertRaises(ValueError):
                recipe_from_dict(bad)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
