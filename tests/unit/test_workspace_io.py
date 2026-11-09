"""Unit tests verifying full-workspace JSON export, inspection, safety snapshots, and atomic restoration."""

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from tared.core.models import CatalogIngredient, Recipe
from tared.storage.sqlite_repo import SQLiteRecipeRepository
from tared.storage.workspace_io import (
    export_workspace_to_dict,
    export_workspace_to_json_file,
    inspect_workspace_file,
    restore_workspace_from_json,
)
from tests.conftest import create_test_recipe


class TestWorkspaceIO(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_workspace.db"
        self.repo = SQLiteRecipeRepository(self.db_path)

        # Seed catalog
        self.repo.save_catalog_ingredient(
            CatalogIngredient("Vanilla Extract", Decimal("45.00"), supplier="Direct Spices")
        )
        self.repo.save_catalog_ingredient(CatalogIngredient("Sugar", Decimal("1.25"), sku="SUG-01"))

        # Seed recipes with revisions
        self.recipe = create_test_recipe()
        self.repo.save_recipe(self.recipe)

        updated_recipe = Recipe(
            id=self.recipe.id,
            name="Benchmark Formulation Rev 2",
            ingredients=self.recipe.ingredients,
            version=2,
        )
        self.repo.save_recipe(updated_recipe)

        # Seed batch runs
        self.repo.record_batch(
            recipe_id=self.recipe.id,
            recipe_version=1,
            batch_number="BAT-WS-001",
            operator="Operator Lead",
            target_weight=Decimal("1000.00"),
            total_weight=Decimal("1000.00"),
            total_cost=Decimal("15.50"),
            notes="Initial calibration run.",
        )

    def tearDown(self) -> None:
        self.repo.close()
        self.temp_dir.cleanup()

    def test_restore_workspace_aborts_on_foreign_key_violation(self) -> None:
        corrupted_data = export_workspace_to_dict(self.repo)
        # Corrupt batches by pointing to a non-existent recipe_version (violating FK constraint)
        corrupted_data["batches"][0]["recipe_version"] = 9999

        corrupted_file = Path(self.temp_dir.name) / "corrupted_fk.json"
        corrupted_file.write_text(json.dumps(corrupted_data), encoding="utf-8")

        with self.assertRaises(Exception) as ctx:
            restore_workspace_from_json(self.repo, corrupted_file)
        self.assertIn("Foreign key integrity check failed", str(ctx.exception))

        # Assert active database was not replaced or corrupted
        batches = self.repo.list_batches(self.recipe.id)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["recipe_version"], 1)

    def test_inspect_workspace_tolerates_in_progress_drafts_without_aborting_restoration(self) -> None:
        data = export_workspace_to_dict(self.repo)
        data["recipes"][0]["ingredients"][0]["percentage"] = "50.00"
        corrupt_rev_items = json.dumps([{"name": "Draft Component", "percentage": "40.00", "cost_per_unit": "1.00"}])
        data["revisions"][0]["ingredients_json"] = corrupt_rev_items

        draft_file = Path(self.temp_dir.name) / "draft_workspace.json"
        draft_file.write_text(json.dumps(data), encoding="utf-8")

        stats = inspect_workspace_file(draft_file)
        self.assertIn(data["recipes"][0]["name"], stats["draft_recipes"])
        self.assertGreaterEqual(len(stats["draft_revisions"]), 1)

        restore_workspace_from_json(self.repo, draft_file)
        restored = self.repo.get_recipe(data["recipes"][0]["id"])
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.ingredients[0].percentage, Decimal("50.00"))

    def test_inspect_workspace_rejects_structural_negative_percentage(self) -> None:
        corrupted_data = export_workspace_to_dict(self.repo)
        corrupted_data["recipes"][0]["ingredients"][0]["percentage"] = "-10.00"

        corrupt_file = Path(self.temp_dir.name) / "corrupt_pct.json"
        corrupt_file.write_text(json.dumps(corrupted_data), encoding="utf-8")

        with self.assertRaises(ValueError) as ctx:
            inspect_workspace_file(corrupt_file)
        self.assertIn("malformed ingredient data", str(ctx.exception))

    def test_atomic_catalog_json_import_rolls_back_on_corrupt_row(self) -> None:
        bad_cat_data = {
            "catalog": [
                {"name": "Good Material", "cost_per_unit": "10.00"},
                {"name": "Corrupt Material", "cost_per_unit": "not-a-number"},
            ]
        }
        bad_file = Path(self.temp_dir.name) / "corrupt_cat.json"
        bad_file.write_text(json.dumps(bad_cat_data), encoding="utf-8")

        from tared.storage.workspace_io import import_catalog_from_json_file

        with self.assertRaises(ValueError):
            import_catalog_from_json_file(self.repo, bad_file)

        # Confirm atomicity: 'Good Material' was not partially inserted
        self.assertIsNone(self.repo.get_catalog_ingredient("Good Material"))

    def test_inspect_workspace_rejects_malformed_ingredients_json(self) -> None:
        bad_rev_data = export_workspace_to_dict(self.repo)
        # Taint ingredients_json to not be a list of dicts
        bad_rev_data["revisions"][0]["ingredients_json"] = json.dumps({"not": "a list"})

        bad_file = Path(self.temp_dir.name) / "bad_json.json"
        bad_file.write_text(json.dumps(bad_rev_data), encoding="utf-8")

        with self.assertRaises(ValueError) as ctx:
            inspect_workspace_file(bad_file)
        self.assertIn("invalid ingredients_json", str(ctx.exception))

    def test_backup_rotation_preserves_three_generations(self) -> None:
        backup_file = Path(self.temp_dir.name) / "rotation_test.json"

        # Generation 1
        export_workspace_to_json_file(self.repo, backup_file, max_rotations=3)
        self.assertTrue(backup_file.is_file())
        self.assertFalse(backup_file.with_name("rotation_test.1.json").exists())

        # Generation 2
        export_workspace_to_json_file(self.repo, backup_file, max_rotations=3)
        self.assertTrue(backup_file.with_name("rotation_test.1.json").is_file())

        # Generation 3
        export_workspace_to_json_file(self.repo, backup_file, max_rotations=3)
        self.assertTrue(backup_file.with_name("rotation_test.1.json").is_file())
        self.assertTrue(backup_file.with_name("rotation_test.2.json").is_file())

        # Generation 4
        export_workspace_to_json_file(self.repo, backup_file, max_rotations=3)
        self.assertTrue(backup_file.with_name("rotation_test.1.json").is_file())
        self.assertTrue(backup_file.with_name("rotation_test.2.json").is_file())
        self.assertTrue(backup_file.with_name("rotation_test.3.json").is_file())

    def test_export_workspace_to_dict_contains_all_domains(self) -> None:
        data = export_workspace_to_dict(self.repo)

        self.assertEqual(data["schema_version"], "1.0")
        self.assertIn("exported_at", data)
        self.assertEqual(len(data["catalog"]), 2)
        self.assertEqual(len(data["recipes"]), 1)
        self.assertEqual(len(data["revisions"]), 2)
        self.assertEqual(len(data["batches"]), 1)

        self.assertEqual(data["batches"][0]["batch_number"], "BAT-WS-001")
        self.assertEqual(data["catalog"][0]["name"], "Sugar")  # Alphabetical NOCASE
        self.assertEqual(data["recipes"][0]["version"], 2)

    def test_export_to_json_file_creates_valid_file_atomically(self) -> None:
        out_file = Path(self.temp_dir.name) / "exports" / "backup.json"
        exported_path = export_workspace_to_json_file(self.repo, out_file)

        self.assertTrue(exported_path.is_file())
        self.assertGreater(exported_path.stat().st_size, 500)

        # Confirm no residual staging files
        staging_files = list(out_file.parent.glob(".tmp_*"))
        self.assertEqual(len(staging_files), 0)

    def test_inspect_workspace_file_returns_accurate_metadata(self) -> None:
        out_file = Path(self.temp_dir.name) / "workspace_inspection.json"
        export_workspace_to_json_file(self.repo, out_file)

        stats = inspect_workspace_file(out_file)
        self.assertEqual(stats["schema_version"], "1.0")
        self.assertEqual(stats["catalog_count"], 2)
        self.assertEqual(stats["recipe_count"], 1)
        self.assertEqual(stats["archived_recipe_count"], 0)
        self.assertEqual(stats["revision_count"], 2)
        self.assertEqual(stats["batch_count"], 1)

    def test_inspect_workspace_file_rejects_invalid_schema(self) -> None:
        bad_file = Path(self.temp_dir.name) / "bad.json"
        bad_file.write_text('{"schema_version": "99.0", "catalog": [], "recipes": []}', encoding="utf-8")

        with self.assertRaises(ValueError) as ctx:
            inspect_workspace_file(bad_file)
        self.assertIn("Unsupported workspace schema version", str(ctx.exception))

    def test_restore_workspace_roundtrip_preserves_all_data_and_creates_backup(self) -> None:
        backup_json = Path(self.temp_dir.name) / "workspace_full.json"
        export_workspace_to_json_file(self.repo, backup_json)

        # Mutate the active database
        self.repo.save_catalog_ingredient(CatalogIngredient("New Contaminant", Decimal("99.99")))
        self.repo.delete_recipe(self.recipe.id)
        self.assertEqual(len(self.repo.list_recipes()), 0)

        # Restore from backup
        safety_dir = Path(self.temp_dir.name) / "safety_snapshots"
        safety_backup = restore_workspace_from_json(self.repo, backup_json, pre_restore_backup_dir=safety_dir)

        self.assertIsNotNone(safety_backup)
        assert safety_backup is not None
        self.assertTrue(safety_backup.is_file())
        self.assertIn("recipes_pre_restore_", safety_backup.name)

        # Verify restored state matches original exactly
        restored_recipes = self.repo.list_recipes()
        self.assertEqual(len(restored_recipes), 1)
        self.assertEqual(restored_recipes[0].name, "Benchmark Formulation Rev 2")
        self.assertEqual(restored_recipes[0].version, 2)

        revisions = self.repo.get_recipe_revisions(self.recipe.id)
        self.assertEqual(len(revisions), 2)

        batches = self.repo.list_batches(self.recipe.id)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["batch_number"], "BAT-WS-001")

        catalog = self.repo.list_catalog_ingredients()
        self.assertEqual(len(catalog), 2)
        self.assertIsNone(self.repo.get_catalog_ingredient("New Contaminant"))

    def test_restore_workspace_in_memory_repository(self) -> None:
        mem_repo = SQLiteRecipeRepository(":memory:")
        # Seed populated recipe with immutable revision to test drop/recreate avoids trigger abort
        populated_recipe = Recipe(
            id="mem-existing-01",
            name="Existing Mem Recipe",
            ingredients=self.recipe.ingredients,
            version=1,
        )
        mem_repo.save_recipe(populated_recipe)
        mem_repo.save_catalog_ingredient(CatalogIngredient("Memory Base", Decimal("10.00")))

        data = export_workspace_to_dict(self.repo)
        safety_path = mem_repo.restore_workspace_data(data)

        self.assertIsNone(safety_path)
        self.assertEqual(len(mem_repo.list_recipes()), 1)
        self.assertEqual(mem_repo.list_recipes()[0].name, "Benchmark Formulation Rev 2")
        self.assertEqual(len(mem_repo.list_catalog_ingredients()), 2)
        mem_repo.close()



    def test_inspect_workspace_rejects_corrupted_batch_record(self) -> None:
        data = export_workspace_to_dict(self.repo)
        data['batches'][0]['target_weight'] = 'not-a-decimal'
        corrupt_file = Path(self.temp_dir.name) / 'corrupt_batch.json'
        corrupt_file.write_text(json.dumps(data), encoding='utf-8')

        with self.assertRaises(ValueError) as ctx:
            inspect_workspace_file(corrupt_file)
        self.assertIn('invalid numeric metrics', str(ctx.exception))

if __name__ == "__main__":
    unittest.main()
