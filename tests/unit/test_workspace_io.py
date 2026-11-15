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
        err_msg = str(ctx.exception)
        self.assertTrue(
            "references non-existent recipe revision" in err_msg
            or "Foreign key integrity check failed" in err_msg
        )

        # Assert active database was not replaced or corrupted
        batches = self.repo.list_batches(self.recipe.id)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["recipe_version"], 1)

    def test_failed_restore_cleans_staged_db_and_wal_sidecars(self) -> None:
        data = export_workspace_to_dict(self.repo)
        # Bypass inspect_workspace_file by calling restore_workspace_data directly with broken FK
        data["batches"][0]["recipe_version"] = 9999
        with self.assertRaises(Exception):
            self.repo.restore_workspace_data(data)

        remnants = list(Path(self.temp_dir.name).glob(".restore_stage_*"))
        self.assertEqual(remnants, [])

    def test_inspect_workspace_tolerates_in_progress_drafts_without_aborting_restoration(self) -> None:
        data = export_workspace_to_dict(self.repo)
        data["recipes"][0]["ingredients"][0]["percentage"] = "50.00"

        # Revision at version 2 reflects the saved draft formulation
        draft_rev_items = [
            {"name": it["name"], "percentage": str(it["percentage"]), "cost_per_unit": str(it["cost_per_unit"])}
            for it in data["recipes"][0]["ingredients"]
        ]
        for rev in data["revisions"]:
            if rev["recipe_id"] == data["recipes"][0]["id"] and rev["version"] == data["recipes"][0]["version"]:
                rev["ingredients_json"] = json.dumps(draft_rev_items)

        draft_file = Path(self.temp_dir.name) / "draft_workspace.json"
        draft_file.write_text(json.dumps(data), encoding="utf-8")

        stats = inspect_workspace_file(draft_file)
        self.assertIn(data["recipes"][0]["name"], stats["draft_recipes"])

        restore_workspace_from_json(self.repo, draft_file)
        restored = self.repo.get_recipe(data["recipes"][0]["id"])
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.ingredients[0].percentage, Decimal("50.00"))

    def test_inspect_workspace_rejects_recipe_contradicting_own_revision(self) -> None:
        data = export_workspace_to_dict(self.repo)
        data["recipes"][0]["ingredients"][0]["percentage"] = "99.99"
        data["recipes"][0]["ingredients"][1]["percentage"] = "0.01"

        corrupt_file = Path(self.temp_dir.name) / "contradictory_revision.json"
        corrupt_file.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaises(ValueError) as ctx:
            inspect_workspace_file(corrupt_file)
        self.assertIn("contradicts its immutable revision snapshot", str(ctx.exception))

    def test_inspect_workspace_rejects_duplicate_batch_numbers(self) -> None:
        data = export_workspace_to_dict(self.repo)
        dupe_batch = dict(data["batches"][0])
        dupe_batch["id"] = "new-dupe-id"
        data["batches"].append(dupe_batch)

        corrupt_file = Path(self.temp_dir.name) / "duplicate_batch_num.json"
        corrupt_file.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaises(ValueError) as ctx:
            inspect_workspace_file(corrupt_file)
        self.assertIn("Duplicate batch number", str(ctx.exception))

    def test_inspect_workspace_rejects_batch_referencing_nonexistent_revision(self) -> None:
        data = export_workspace_to_dict(self.repo)
        data["batches"][0]["recipe_version"] = 999

        corrupt_file = Path(self.temp_dir.name) / "unlinked_batch.json"
        corrupt_file.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaises(ValueError) as ctx:
            inspect_workspace_file(corrupt_file)
        self.assertIn("references non-existent recipe revision", str(ctx.exception))

    def test_inspect_workspace_rejects_batch_with_malformed_items_json(self) -> None:
        data = export_workspace_to_dict(self.repo)
        data["batches"][0]["items_json"] = "not-valid-json{"

        corrupt_file = Path(self.temp_dir.name) / "corrupt_items_json.json"
        corrupt_file.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaises(ValueError) as ctx:
            inspect_workspace_file(corrupt_file)
        self.assertIn("malformed items_json", str(ctx.exception))

    def test_inspect_workspace_rejects_revision_missing_recipe_id(self) -> None:
        data = export_workspace_to_dict(self.repo)
        data["revisions"][0]["recipe_id"] = ""

        corrupt_file = Path(self.temp_dir.name) / "missing_rec_id.json"
        corrupt_file.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaises(ValueError) as ctx:
            inspect_workspace_file(corrupt_file)
        self.assertIn("missing required 'recipe_id'", str(ctx.exception))

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

        self.assertIsNone(self.repo.get_catalog_ingredient("Good Material"))

    def test_inspect_workspace_rejects_malformed_ingredients_json(self) -> None:
        bad_rev_data = export_workspace_to_dict(self.repo)
        bad_rev_data["revisions"][0]["ingredients_json"] = json.dumps({"not": "a list"})

        bad_file = Path(self.temp_dir.name) / "bad_json.json"
        bad_file.write_text(json.dumps(bad_rev_data), encoding="utf-8")

        with self.assertRaises(ValueError) as ctx:
            inspect_workspace_file(bad_file)
        self.assertIn("invalid ingredients_json", str(ctx.exception))

    def test_backup_rotation_preserves_three_generations(self) -> None:
        backup_file = Path(self.temp_dir.name) / "rotation_test.json"

        export_workspace_to_json_file(self.repo, backup_file, max_rotations=3)
        self.assertTrue(backup_file.is_file())
        self.assertFalse(backup_file.with_name("rotation_test.1.json").exists())

        export_workspace_to_json_file(self.repo, backup_file, max_rotations=3)
        self.assertTrue(backup_file.with_name("rotation_test.1.json").is_file())

        export_workspace_to_json_file(self.repo, backup_file, max_rotations=3)
        self.assertTrue(backup_file.with_name("rotation_test.1.json").is_file())
        self.assertTrue(backup_file.with_name("rotation_test.2.json").is_file())

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
        self.assertEqual(data["catalog"][0]["name"], "Sugar")
        self.assertEqual(data["recipes"][0]["version"], 2)

    def test_export_to_json_file_creates_valid_file_atomically(self) -> None:
        out_file = Path(self.temp_dir.name) / "exports" / "backup.json"
        exported_path = export_workspace_to_json_file(self.repo, out_file)

        self.assertTrue(exported_path.is_file())
        self.assertGreater(exported_path.stat().st_size, 500)

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

        self.repo.save_catalog_ingredient(CatalogIngredient("New Contaminant", Decimal("99.99")))
        self.repo.delete_recipe(self.recipe.id)
        self.assertEqual(len(self.repo.list_recipes()), 0)

        safety_dir = Path(self.temp_dir.name) / "safety_snapshots"
        safety_backup = restore_workspace_from_json(self.repo, backup_json, pre_restore_backup_dir=safety_dir)

        self.assertIsNotNone(safety_backup)
        assert safety_backup is not None
        self.assertTrue(safety_backup.is_file())
        self.assertIn("recipes_pre_restore_", safety_backup.name)

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

    def test_workspace_roundtrip_preserves_explicit_draft_without_backfilling_revision(self) -> None:
        from tared.core.models import Ingredient

        draft_rec = Recipe(
            id="rec-ws-draft",
            name="Draft Only Formula",
            version=1,
            is_draft=True,
            ingredients=(Ingredient("Base", Decimal("50.00")),),
        )
        self.repo.save_recipe(draft_rec)

        backup_file = Path(self.temp_dir.name) / "with_draft.json"
        export_workspace_to_json_file(self.repo, backup_file)

        inspect_workspace_file(backup_file)
        restore_workspace_from_json(self.repo, backup_file)

        restored = self.repo.get_recipe("rec-ws-draft")
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertTrue(restored.is_draft)
        self.assertEqual(len(self.repo.get_recipe_revisions("rec-ws-draft")), 0)

    def test_inspect_workspace_parses_string_is_draft_flags_safely(self) -> None:
        data = export_workspace_to_dict(self.repo)
        data["recipes"][0]["is_draft"] = "false"
        valid_file = Path(self.temp_dir.name) / "string_false_draft.json"
        valid_file.write_text(json.dumps(data), encoding="utf-8")
        stats = inspect_workspace_file(valid_file)
        self.assertEqual(stats["recipe_count"], 1)

        data["recipes"][0]["is_draft"] = "invalid-bool"
        invalid_file = Path(self.temp_dir.name) / "invalid_bool_draft.json"
        invalid_file.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            inspect_workspace_file(invalid_file)

    def test_inspect_workspace_rejects_version_below_one(self) -> None:
        data = export_workspace_to_dict(self.repo)
        data["recipes"][0]["version"] = 0
        bad_rec_ver = Path(self.temp_dir.name) / "bad_rec_ver.json"
        bad_rec_ver.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            inspect_workspace_file(bad_rec_ver)
        self.assertIn("must be >= 1", str(ctx.exception))

        data2 = export_workspace_to_dict(self.repo)
        data2["revisions"][0]["version"] = 0
        bad_rev_ver = Path(self.temp_dir.name) / "bad_rev_ver.json"
        bad_rev_ver.write_text(json.dumps(data2), encoding="utf-8")
        with self.assertRaises(ValueError) as ctx2:
            inspect_workspace_file(bad_rev_ver)
        self.assertIn("must be >= 1", str(ctx2.exception))

    def test_inspect_workspace_rejects_recipe_metadata_contradicting_revision(self) -> None:
        data = export_workspace_to_dict(self.repo)
        data["recipes"][0]["name"] = "Tampered Name"
        bad_name_file = Path(self.temp_dir.name) / "bad_name_parity.json"
        bad_name_file.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            inspect_workspace_file(bad_name_file)
        self.assertIn("contradicts its immutable revision snapshot", str(ctx.exception))

        data2 = export_workspace_to_dict(self.repo)
        data2["recipes"][0]["description"] = "Tampered SOP"
        bad_desc_file = Path(self.temp_dir.name) / "bad_desc_parity.json"
        bad_desc_file.write_text(json.dumps(data2), encoding="utf-8")
        with self.assertRaises(ValueError) as ctx2:
            inspect_workspace_file(bad_desc_file)
        self.assertIn("contradicts its immutable revision snapshot", str(ctx2.exception))


    def test_import_catalog_null_fields_normalized_to_empty_strings(self) -> None:
        from tared.storage.workspace_io import import_catalog_from_json_file

        cat_data = {
            "catalog": [
                {
                    "name": "Nullable Material",
                    "cost_per_unit": "5.50",
                    "supplier": None,
                    "sku": None,
                    "notes": None,
                    "updated_at": None,
                }
            ]
        }
        json_file = Path(self.temp_dir.name) / "null_cat.json"
        json_file.write_text(json.dumps(cat_data), encoding="utf-8")

        import_catalog_from_json_file(self.repo, json_file)
        item = self.repo.get_catalog_ingredient("Nullable Material")
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.supplier, "")
        self.assertEqual(item.sku, "")
        self.assertEqual(item.notes, "")

    def test_inspect_workspace_rejects_revision_referencing_nonexistent_recipe(self) -> None:
        data = export_workspace_to_dict(self.repo)
        orphan_id = "non-existent-rec-id"
        data["revisions"][0]["recipe_id"] = orphan_id
        # Point batches to the remaining valid revision so revision orphan check is reached
        valid_rev = data["revisions"][1]
        data["batches"][0]["recipe_id"] = valid_rev["recipe_id"]
        data["batches"][0]["recipe_version"] = valid_rev["version"]
        corrupt_file = Path(self.temp_dir.name) / "orphan_rev.json"
        corrupt_file.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaises(ValueError) as ctx:
            inspect_workspace_file(corrupt_file)
        self.assertIn("references non-existent recipe ID", str(ctx.exception))

    def test_inspect_workspace_validates_batches_against_recipes_when_revisions_empty(self) -> None:
        data = export_workspace_to_dict(self.repo)
        data["revisions"] = []

        # Batch referencing v1 while recipe is at v2 cannot be backfilled safely
        mismatch_file = Path(self.temp_dir.name) / "mismatch_legacy_no_rev.json"
        mismatch_file.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError) as ver_ctx:
            inspect_workspace_file(mismatch_file)
        self.assertIn("does not match recipe", str(ver_ctx.exception))

        # Matching recipe version passes inspection and restores cleanly
        data["batches"][0]["recipe_version"] = data["recipes"][0]["version"]
        valid_file = Path(self.temp_dir.name) / "legacy_no_rev.json"
        valid_file.write_text(json.dumps(data), encoding="utf-8")

        stats = inspect_workspace_file(valid_file)
        self.assertEqual(stats["batch_count"], 1)
        restore_workspace_from_json(self.repo, valid_file)

        data["batches"][0]["recipe_id"] = "non-existent-rec-id"
        corrupt_file = Path(self.temp_dir.name) / "bad_legacy_batch.json"
        corrupt_file.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            inspect_workspace_file(corrupt_file)
        self.assertIn("references non-existent recipe ID", str(ctx.exception))

    def test_restore_workspace_normalizes_null_optional_fields(self) -> None:
        data = export_workspace_to_dict(self.repo)
        rec = data["recipes"][0]
        rec["description"] = None
        rec["updated_at"] = None
        for rev in data["revisions"]:
            if rev["recipe_id"] == rec["id"] and rev["version"] == rec["version"]:
                rev["description"] = None
            rev["created_at"] = None
        data["batches"][0]["notes"] = None
        data["batches"][0]["created_at"] = None
        data["batches"][0]["precision"] = None
        data["catalog"][0]["supplier"] = None
        data["catalog"][0]["sku"] = None
        data["catalog"][0]["notes"] = None
        data["catalog"][0]["updated_at"] = None

        null_ws = Path(self.temp_dir.name) / "null_optional_fields.json"
        null_ws.write_text(json.dumps(data), encoding="utf-8")

        stats = inspect_workspace_file(null_ws)
        self.assertEqual(stats["recipe_count"], 1)

        restore_workspace_from_json(self.repo, null_ws)
        restored = self.repo.get_recipe(self.recipe.id)
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.description, "")
        self.assertEqual(restored.updated_at, "")

        batches = self.repo.list_batches(self.recipe.id)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["notes"], "")
        self.assertEqual(batches[0]["precision"], "0.01")

        cat_item = self.repo.get_catalog_ingredient("Sugar")
        self.assertIsNotNone(cat_item)
        assert cat_item is not None
        self.assertEqual(cat_item.supplier, "")
        self.assertEqual(cat_item.sku, "")

    def test_import_catalog_rejects_null_name_instead_of_literal_none(self) -> None:
        from tared.storage.workspace_io import import_catalog_from_json_file

        cat_data = {"catalog": [{"name": None, "cost_per_unit": "5.50"}]}
        json_file = Path(self.temp_dir.name) / "null_name_cat.json"
        json_file.write_text(json.dumps(cat_data), encoding="utf-8")

        with self.assertRaises(ValueError):
            import_catalog_from_json_file(self.repo, json_file)
        self.assertIsNone(self.repo.get_catalog_ingredient("None"))

    def test_inspect_workspace_normalizes_null_costs_and_rejects_null_names_booleans_and_bad_precision(self) -> None:
        base = export_workspace_to_dict(self.repo)
        base["catalog"][0]["cost_per_unit"] = None
        base["recipes"][0]["ingredients"][0]["cost_per_unit"] = None
        for rev in base["revisions"]:
            if rev["recipe_id"] == base["recipes"][0]["id"] and rev["version"] == base["recipes"][0]["version"]:
                items = json.loads(rev["ingredients_json"])
                items[0]["cost_per_unit"] = None
                rev["ingredients_json"] = json.dumps(items)
        null_cost_ws = Path(self.temp_dir.name) / "null_costs_ws.json"
        null_cost_ws.write_text(json.dumps(base), encoding="utf-8")
        stats = inspect_workspace_file(null_cost_ws)
        self.assertEqual(stats["catalog_count"], 2)
        restore_workspace_from_json(self.repo, null_cost_ws)

        bad_cat = export_workspace_to_dict(self.repo)
        bad_cat["catalog"][0]["name"] = None
        bad_cat_file = Path(self.temp_dir.name) / "null_cat_name_ws.json"
        bad_cat_file.write_text(json.dumps(bad_cat), encoding="utf-8")
        with self.assertRaises(ValueError):
            inspect_workspace_file(bad_cat_file)

        bad_rec = export_workspace_to_dict(self.repo)
        bad_rec["recipes"][0]["name"] = None
        bad_rec_file = Path(self.temp_dir.name) / "null_rec_name_ws.json"
        bad_rec_file.write_text(json.dumps(bad_rec), encoding="utf-8")
        with self.assertRaises(ValueError):
            inspect_workspace_file(bad_rec_file)

        bad_batch = export_workspace_to_dict(self.repo)
        bad_batch["batches"][0]["batch_number"] = None
        bad_batch_file = Path(self.temp_dir.name) / "null_batch_num_ws.json"
        bad_batch_file.write_text(json.dumps(bad_batch), encoding="utf-8")
        with self.assertRaises(ValueError):
            inspect_workspace_file(bad_batch_file)

        bad_bool_ver = export_workspace_to_dict(self.repo)
        bad_bool_ver["recipes"][0]["version"] = True
        bad_bool_file = Path(self.temp_dir.name) / "bool_ver_ws.json"
        bad_bool_file.write_text(json.dumps(bad_bool_ver), encoding="utf-8")
        with self.assertRaises(ValueError):
            inspect_workspace_file(bad_bool_file)

        bad_prec = export_workspace_to_dict(self.repo)
        bad_prec["batches"][0]["precision"] = "0"
        bad_prec_file = Path(self.temp_dir.name) / "zero_prec_ws.json"
        bad_prec_file.write_text(json.dumps(bad_prec), encoding="utf-8")
        with self.assertRaises(ValueError):
            inspect_workspace_file(bad_prec_file)

    def test_inspect_workspace_rejects_missing_revision_name_and_batch_required_keys(self) -> None:
        missing_rev_name = export_workspace_to_dict(self.repo)
        del missing_rev_name["revisions"][0]["name"]
        rev_file = Path(self.temp_dir.name) / "missing_rev_name.json"
        rev_file.write_text(json.dumps(missing_rev_name), encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            inspect_workspace_file(rev_file)
        self.assertIn("'name' must be a non-empty string", str(ctx.exception))

        for req_key in ("recipe_version", "target_weight", "total_weight", "total_cost"):
            missing_batch_key = export_workspace_to_dict(self.repo)
            del missing_batch_key["batches"][0][req_key]
            b_file = Path(self.temp_dir.name) / f"missing_{req_key}.json"
            b_file.write_text(json.dumps(missing_batch_key), encoding="utf-8")
            with self.assertRaises(ValueError) as b_ctx:
                inspect_workspace_file(b_file)
            self.assertIn(req_key, str(b_ctx.exception))

    def test_import_catalog_rejects_dict_without_catalog_key_and_accepts_list(self) -> None:
        from tared.storage.workspace_io import import_catalog_from_json_file

        recipe_like_file = Path(self.temp_dir.name) / "not_a_catalog.json"
        recipe_like_file.write_text(
            json.dumps({"version": "1.0", "name": "Some Recipe", "ingredients": []}),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError) as ctx:
            import_catalog_from_json_file(self.repo, recipe_like_file)
        self.assertIn("missing required 'catalog'", str(ctx.exception))

        bare_list_file = Path(self.temp_dir.name) / "bare_list_catalog.json"
        bare_list_file.write_text(
            json.dumps([{"name": "Pectin", "cost_per_unit": "19.25"}]),
            encoding="utf-8",
        )
        count = import_catalog_from_json_file(self.repo, bare_list_file)
        self.assertEqual(count, 1)
        pectin = self.repo.get_catalog_ingredient("Pectin")
        self.assertIsNotNone(pectin)
        assert pectin is not None
        self.assertEqual(pectin.cost_per_unit, Decimal("19.25"))

    def test_restore_strips_padded_ids_and_rejects_empty_revisions_batch_to_draft(self) -> None:
        from tared.core.models import Ingredient

        data = export_workspace_to_dict(self.repo)
        rec_id = data["recipes"][0]["id"]
        data["recipes"][0]["id"] = f"  {rec_id}  "
        for rev in data["revisions"]:
            if rev["recipe_id"] == rec_id:
                rev["recipe_id"] = f"{rec_id} "
        data["batches"][0]["recipe_id"] = f" {rec_id}"
        data["batches"][0]["batch_number"] = "  BAT-WS-001  "

        padded_file = Path(self.temp_dir.name) / "padded_ids.json"
        padded_file.write_text(json.dumps(data), encoding="utf-8")

        restore_workspace_from_json(self.repo, padded_file)
        self.assertTrue(self.repo.batch_exists("BAT-WS-001"))
        self.assertIsNotNone(self.repo.get_recipe(rec_id))

        # Empty revisions pointing to a draft recipe must fail pre-flight inspection
        draft_repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(draft_repo.close)
        draft_rec = Recipe(
            id="rec-draft-fk",
            name="Draft Only",
            version=1,
            is_draft=True,
            ingredients=(Ingredient("Water", Decimal("50.00")),),
        )
        draft_repo.save_recipe(draft_rec)
        bad_draft_ws = export_workspace_to_dict(draft_repo)
        bad_draft_ws["revisions"] = []
        bad_draft_ws["batches"] = [
            {
                "id": "b-draft-1",
                "recipe_id": "rec-draft-fk",
                "recipe_version": 1,
                "batch_number": "BAT-DRAFT-BAD",
                "operator": "Op",
                "target_weight": "100.00",
                "total_weight": "100.00",
                "total_cost": "1.00",
                "created_at": "2026-01-01T00:00:00Z",
            }
        ]
        bad_draft_file = Path(self.temp_dir.name) / "empty_rev_draft_batch.json"
        bad_draft_file.write_text(json.dumps(bad_draft_ws), encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            inspect_workspace_file(bad_draft_file)
        self.assertIn("draft recipe", str(ctx.exception))

    def test_inspect_workspace_rejects_duplicate_catalog_entries_and_preserves_unicode(self) -> None:
        dup_name = export_workspace_to_dict(self.repo)
        dup_name["catalog"].append({"id": "dup-cat-id-1", "name": "sugar", "cost_per_unit": "2.00"})
        dup_name_file = Path(self.temp_dir.name) / "dup_cat_name.json"
        dup_name_file.write_text(json.dumps(dup_name), encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            inspect_workspace_file(dup_name_file)
        self.assertIn("Duplicate catalog material name", str(ctx.exception))

        dup_id = export_workspace_to_dict(self.repo)
        dup_id["catalog"].append({"id": dup_id["catalog"][0]["id"], "name": "Salt", "cost_per_unit": "0.50"})
        dup_id_file = Path(self.temp_dir.name) / "dup_cat_id.json"
        dup_id_file.write_text(json.dumps(dup_id), encoding="utf-8")
        with self.assertRaises(ValueError) as id_ctx:
            inspect_workspace_file(dup_id_file)
        self.assertIn("Duplicate catalog ID", str(id_ctx.exception))

        unicode_ok = export_workspace_to_dict(self.repo)
        unicode_ok["catalog"].append({"id": "uni-1", "name": "Äpfel", "cost_per_unit": "3.00"})
        unicode_ok["catalog"].append({"id": "uni-2", "name": "äpfel", "cost_per_unit": "3.50"})
        uni_file = Path(self.temp_dir.name) / "unicode_cat.json"
        uni_file.write_text(json.dumps(unicode_ok), encoding="utf-8")
        stats = inspect_workspace_file(uni_file)
        self.assertEqual(stats["catalog_count"], 4)
        restore_workspace_from_json(self.repo, uni_file)

    def test_restore_workspace_strips_padded_recipe_and_revision_names(self) -> None:
        data = export_workspace_to_dict(self.repo)
        data["recipes"][0]["name"] = f"  {data['recipes'][0]['name']}  "
        for rev in data["revisions"]:
            if rev["recipe_id"] == data["recipes"][0]["id"] and rev["version"] == data["recipes"][0]["version"]:
                rev["name"] = f"  {rev['name']}  "

        padded_file = Path(self.temp_dir.name) / "padded_names.json"
        padded_file.write_text(json.dumps(data), encoding="utf-8")

        restore_workspace_from_json(self.repo, padded_file)
        restored = self.repo.get_recipe(self.recipe.id)
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.name, "Benchmark Formulation Rev 2")

    def test_restore_empty_revisions_normalizes_backfilled_ingredients_json(self) -> None:
        data = export_workspace_to_dict(self.repo)
        data["revisions"] = []
        data["batches"][0]["recipe_version"] = data["recipes"][0]["version"]
        data["recipes"][0]["ingredients"][0]["name"] = "  Material A  "
        data["recipes"][0]["ingredients"][0]["cost_per_unit"] = None

        legacy_file = Path(self.temp_dir.name) / "legacy_backfill_norm.json"
        legacy_file.write_text(json.dumps(data), encoding="utf-8")

        restore_workspace_from_json(self.repo, legacy_file)
        revs = self.repo.get_recipe_revisions(self.recipe.id)
        self.assertEqual(len(revs), 1)
        rev_ings = json.loads(revs[0]["ingredients_json"])
        self.assertEqual(rev_ings[0]["name"], "Material A")
        self.assertEqual(rev_ings[0]["cost_per_unit"], "0.00")

    def test_inspect_workspace_handles_null_items_json_and_rejects_bad_metrics_or_float_versions(self) -> None:
        base = export_workspace_to_dict(self.repo)
        base["batches"][0]["items_json"] = None
        null_items_file = Path(self.temp_dir.name) / "null_items_json.json"
        null_items_file.write_text(json.dumps(base), encoding="utf-8")
        self.assertEqual(inspect_workspace_file(null_items_file)["batch_count"], 1)
        restore_workspace_from_json(self.repo, null_items_file)

        for bad_items in ("[null]", '["not-a-dict"]'):
            bad = export_workspace_to_dict(self.repo)
            bad["batches"][0]["items_json"] = bad_items
            f = Path(self.temp_dir.name) / "bad_items_array.json"
            f.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ValueError):
                inspect_workspace_file(f)

        for field, val in (("target_weight", "0"), ("total_weight", "-10"), ("total_cost", "-1")):
            bad_m = export_workspace_to_dict(self.repo)
            bad_m["batches"][0][field] = val
            f = Path(self.temp_dir.name) / f"bad_{field}.json"
            f.write_text(json.dumps(bad_m), encoding="utf-8")
            with self.assertRaises(ValueError):
                inspect_workspace_file(f)

        bad_f = export_workspace_to_dict(self.repo)
        bad_f["recipes"][0]["version"] = 1.5
        f_ver = Path(self.temp_dir.name) / "float_ver.json"
        f_ver.write_text(json.dumps(bad_f), encoding="utf-8")
        with self.assertRaises(ValueError):
            inspect_workspace_file(f_ver)

    def test_import_catalog_counts_unique_materials_only(self) -> None:
        from tared.storage.workspace_io import import_catalog_from_json_file

        cat_file = Path(self.temp_dir.name) / "dup_cat_import.json"
        cat_file.write_text(
            json.dumps(
                {
                    "catalog": [
                        {"name": "Pectin", "cost_per_unit": "18.00"},
                        {"name": "PECTIN", "cost_per_unit": "19.00"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        count = import_catalog_from_json_file(self.repo, cat_file)
        self.assertEqual(count, 1)
        pectin = self.repo.get_catalog_ingredient("Pectin")
        assert pectin is not None
        self.assertEqual(pectin.cost_per_unit, Decimal("19.00"))


if __name__ == "__main__":
    unittest.main()
