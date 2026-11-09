"""Unit tests for SQLite repository revision snapshots, immutability triggers, audit batches, and migrations."""

import json
import sqlite3
import unittest
from decimal import Decimal

from tared.core.models import CatalogIngredient, Ingredient, Recipe
from tared.core.service import RecipeService
from tared.storage.base import (
    DuplicateBatchNumberError,
    RecipeNotFoundError,
    StorageError,
)
from tared.storage.json_repo import (
    export_recipe_to_json,
    import_recipe_from_json,
)
from tared.storage.sqlite_repo import SQLiteRecipeRepository
from tests.conftest import create_test_recipe


class TestSQLiteRecipeRepository(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = SQLiteRecipeRepository(":memory:")
        self.recipe = create_test_recipe()

    def tearDown(self) -> None:
        self.repo.close()

    def test_save_and_retrieve_recipe_with_version_and_snapshot_archive(self) -> None:
        recipe_v1 = Recipe(
            id=self.recipe.id,
            name=self.recipe.name,
            ingredients=self.recipe.ingredients,
            version=1,
            updated_at="2026-11-06T10:00:00Z",
        )
        self.repo.save_recipe(recipe_v1)

        recipe_v2 = Recipe(
            id=self.recipe.id,
            name="Revised Formulation",
            ingredients=(Ingredient(name="Single Base", percentage=Decimal("100.00")),),
            version=2,
            updated_at="2026-11-06T11:00:00Z",
        )
        self.repo.save_recipe(recipe_v2)

        current = self.repo.get_recipe(self.recipe.id)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.version, 2)
        self.assertEqual(current.name, "Revised Formulation")

        revisions = self.repo.get_recipe_revisions(self.recipe.id)
        self.assertEqual(len(revisions), 2)
        self.assertEqual(revisions[0]["version"], 2)
        self.assertEqual(revisions[1]["version"], 1)
        self.assertIn("Revised Formulation", revisions[0]["name"])
        self.assertIn("Standard Benchmark Formulation", revisions[1]["name"])

    def test_batch_immutability_triggers_prevent_update_and_delete(self) -> None:
        self.repo.save_recipe(self.recipe)
        batch_id = self.repo.record_batch(
            recipe_id=self.recipe.id,
            recipe_version=1,
            batch_number="BAT-LOCK-001",
            operator="Operator Alpha",
            target_weight=Decimal("100.00"),
            total_weight=Decimal("100.00"),
            total_cost=Decimal("2.50"),
        )
        conn = self.repo._get_connection()

        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE batches SET operator = 'Tampered' WHERE id = ?;", (batch_id,))

        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM batches WHERE id = ?;", (batch_id,))

    def test_revision_immutability_triggers_prevent_update_and_delete(self) -> None:
        self.repo.save_recipe(self.recipe)
        conn = self.repo._get_connection()

        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE recipe_revisions SET name = 'Tampered' WHERE recipe_id = ?;", (self.recipe.id,))

        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM recipe_revisions WHERE recipe_id = ?;", (self.recipe.id,))

    def test_record_batch_stores_precision_and_items_with_revision_fk(self) -> None:
        self.repo.save_recipe(self.recipe)
        dummy_items = [
            {"ingredient_name": "Material A", "scaled_weight": "290.00", "cumulative_tare": "290.00", "cost": "1.45"},
            {"ingredient_name": "Material B", "scaled_weight": "710.00", "cumulative_tare": "1000.00", "cost": "2.13"},
        ]
        batch_id = self.repo.record_batch(
            recipe_id=self.recipe.id,
            recipe_version=1,
            batch_number="BAT-AUDIT-TEST-001",
            operator="Chemist Sarah",
            precision=Decimal("0.001"),
            target_weight=Decimal("1000.000"),
            total_weight=Decimal("1000.000"),
            total_cost=Decimal("3.58"),
            items=dummy_items,
            notes="Scale calibrated against certified weights.",
        )
        self.assertTrue(len(batch_id) > 0)

        batches = self.repo.list_batches(recipe_id=self.recipe.id)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["batch_number"], "BAT-AUDIT-TEST-001")
        self.assertEqual(batches[0]["precision"], "0.001")
        saved_items = json.loads(batches[0]["items_json"])
        self.assertEqual(len(saved_items), 2)
        self.assertEqual(saved_items[0]["ingredient_name"], "Material A")

    def test_record_batch_rejects_missing_revision_foreign_key(self) -> None:
        self.repo.save_recipe(self.recipe)
        with self.assertRaises(RecipeNotFoundError):
            self.repo.record_batch(
                recipe_id=self.recipe.id,
                recipe_version=999,
                batch_number="BAT-FAIL-001",
                operator="Operator X",
                target_weight=Decimal("100.00"),
                total_weight=Decimal("100.00"),
                total_cost=Decimal("1.00"),
            )

    def test_record_batch_rejects_duplicate_batch_number(self) -> None:
        self.repo.save_recipe(self.recipe)
        self.repo.record_batch(
            recipe_id=self.recipe.id,
            recipe_version=1,
            batch_number="BAT-DUP-001",
            operator="Operator 1",
            target_weight=Decimal("100.00"),
            total_weight=Decimal("100.00"),
            total_cost=Decimal("1.00"),
        )
        with self.assertRaises(DuplicateBatchNumberError):
            self.repo.record_batch(
                recipe_id=self.recipe.id,
                recipe_version=1,
                batch_number="BAT-DUP-001",
                operator="Operator 2",
                target_weight=Decimal("100.00"),
                total_weight=Decimal("100.00"),
                total_cost=Decimal("1.00"),
            )

    def test_batch_exists_query(self) -> None:
        self.assertFalse(self.repo.batch_exists("NON-EXISTENT"))
        self.repo.save_recipe(self.recipe)
        self.repo.record_batch(
            recipe_id=self.recipe.id,
            recipe_version=1,
            batch_number="BAT-EXISTS-01",
            operator="Operator 1",
            target_weight=Decimal("100.00"),
            total_weight=Decimal("100.00"),
            total_cost=Decimal("1.00"),
        )
        self.assertTrue(self.repo.batch_exists("BAT-EXISTS-01"))
        self.assertFalse(self.repo.batch_exists("BAT-EXISTS-02"))

    def test_delete_recipe_archives_while_preserving_audit_revisions_and_batches(self) -> None:
        self.repo.save_recipe(self.recipe)
        self.repo.record_batch(
            recipe_id=self.recipe.id,
            recipe_version=1,
            batch_number="BAT-PRESERVE-001",
            operator="Operator 1",
            target_weight=Decimal("100.00"),
            total_weight=Decimal("100.00"),
            total_cost=Decimal("1.00"),
        )
        self.assertEqual(len(self.repo.list_recipes()), 1)

        deleted = self.repo.delete_recipe(self.recipe.id)
        self.assertTrue(deleted)
        self.assertIsNone(self.repo.get_recipe(self.recipe.id))
        self.assertEqual(len(self.repo.list_recipes()), 0)

        revisions = self.repo.get_recipe_revisions(self.recipe.id)
        self.assertEqual(len(revisions), 1)
        batches = self.repo.list_batches(self.recipe.id)
        self.assertEqual(len(batches), 1)

    def test_migration_backfills_missing_revisions(self) -> None:
        conn = self.repo._get_connection()
        conn.execute(
            "INSERT INTO recipes (id, name, description, version, updated_at, is_archived) "
            "VALUES ('legacy-1', 'Legacy Formula', '', 1, '2025-01-01', 0);"
        )
        conn.execute(
            "INSERT INTO ingredients (recipe_id, sort_order, name, percentage, cost_per_unit) "
            "VALUES ('legacy-1', 0, 'Base', '100.00', '0.00');"
        )
        self.assertEqual(len(self.repo.get_recipe_revisions("legacy-1")), 0)

        # Force migration block to execute
        conn.execute("PRAGMA user_version = 0;")
        self.repo._init_db()

        backfilled = self.repo.get_recipe_revisions("legacy-1")
        self.assertEqual(len(backfilled), 1)
        self.assertEqual(backfilled[0]["name"], "Legacy Formula")

    def test_save_recipe_equivalent_decimals_succeeds_without_integrity_violation(self) -> None:
        recipe_v1 = Recipe(
            id=self.recipe.id,
            name=self.recipe.name,
            ingredients=(
                Ingredient(name="Base", percentage=Decimal("10.00"), cost_per_unit=Decimal("3.00")),
                Ingredient(name="Filler", percentage=Decimal("90.00"), cost_per_unit=Decimal("1.00")),
            ),
            version=1,
        )
        self.repo.save_recipe(recipe_v1)

        # Candidate with equivalent Decimals formatted differently ("10.0" and "3")
        candidate_same_rev = Recipe(
            id=self.recipe.id,
            name=self.recipe.name,
            ingredients=(
                Ingredient(name="Base", percentage=Decimal("10.0"), cost_per_unit=Decimal("3")),
                Ingredient(name="Filler", percentage=Decimal("90"), cost_per_unit=Decimal("1.000")),
            ),
            version=1,
        )
        # Must not raise StorageError: Revision integrity violation
        self.repo.save_recipe(candidate_same_rev)

        retrieved = self.repo.get_recipe(self.recipe.id)
        self.assertIsNotNone(retrieved)
        assert retrieved is not None
        self.assertEqual(retrieved.version, 1)

    def test_legacy_snapshot_stale_cost_bumps_version_gracefully(self) -> None:
        service = RecipeService(self.repo)

        recipe_v1 = Recipe(
            id="legacy-recipe-01",
            name="Legacy Formulation",
            ingredients=(Ingredient(name="Base", percentage=Decimal("100.00"), cost_per_unit=Decimal("1.00")),),
            version=1,
        )
        service.save_recipe(recipe_v1)

        # Simulate legacy database inconsistency where ingredients table had a price edit to 2.00
        # while version was left at 1 and snapshot remained at 1.00
        conn = self.repo._get_connection()
        conn.execute("UPDATE ingredients SET cost_per_unit = '2.00' WHERE recipe_id = 'legacy-recipe-01';")

        current = self.repo.get_recipe("legacy-recipe-01")
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.ingredients[0].cost_per_unit, Decimal("2.00"))
        self.assertEqual(current.version, 1)

        saved_v2 = service.save_recipe(current)
        self.assertEqual(saved_v2.version, 2)
        self.assertEqual(saved_v2.ingredients[0].cost_per_unit, Decimal("2.00"))

    def test_save_recipe_same_version_identical_content_allowed(self) -> None:
        self.repo.save_recipe(self.recipe)
        initial_revisions = self.repo.get_recipe_revisions(self.recipe.id)
        self.assertEqual(len(initial_revisions), 1)

        self.repo.save_recipe(self.recipe)
        current = self.repo.get_recipe(self.recipe.id)
        assert current is not None
        self.assertEqual(current.version, self.recipe.version)

    def test_save_recipe_same_version_modified_content_raises_collision_error(self) -> None:
        self.repo.save_recipe(self.recipe)

        updated_ings = tuple(
            Ingredient(name=ing.name, percentage=ing.percentage, cost_per_unit=Decimal("99.50"))
            for ing in self.recipe.ingredients
        )
        conflicting_recipe = Recipe(
            id=self.recipe.id,
            name=self.recipe.name,
            ingredients=updated_ings,
            version=self.recipe.version,
            updated_at="2026-11-07T10:00:00Z",
        )

        with self.assertRaises(StorageError):
            self.repo.save_recipe(conflicting_recipe)

    def test_sqlite_uri_parameter_mode_memory_support(self) -> None:
        uri_repo = SQLiteRecipeRepository("file:mem_test_uri?mode=memory&cache=shared")
        try:
            uri_repo.save_catalog_ingredient(CatalogIngredient("URI Material", Decimal("12.00")))
            item = uri_repo.get_catalog_ingredient("URI Material")
            self.assertIsNotNone(item)
            assert item is not None
            self.assertEqual(item.cost_per_unit, Decimal("12.00"))
        finally:
            uri_repo.close()

    def test_reset_workspace_data_clears_all_tables_with_backup(self) -> None:
        self.repo.save_recipe(self.recipe)
        self.repo.save_catalog_ingredient(CatalogIngredient("Sugar", Decimal("1.25")))
        self.repo.record_batch(
            recipe_id=self.recipe.id,
            recipe_version=1,
            batch_number="BAT-RESET-01",
            operator="Op",
            target_weight=Decimal("100.00"),
            total_weight=Decimal("100.00"),
            total_cost=Decimal("1.00"),
        )
        self.assertEqual(len(self.repo.list_recipes()), 1)
        self.assertEqual(len(self.repo.list_catalog_ingredients()), 1)
        self.assertEqual(len(self.repo.list_batches()), 1)

        self.repo.reset_workspace_data()

        self.assertEqual(len(self.repo.list_recipes()), 0)
        self.assertEqual(len(self.repo.list_catalog_ingredients()), 0)
        self.assertEqual(len(self.repo.list_batches()), 0)
        self.assertEqual(len(self.repo.list_all_revisions()), 0)

    def test_list_recipes_isolates_corrupted_recipe_without_raising(self) -> None:
        self.repo.save_recipe(self.recipe)
        conn = self.repo._get_connection()
        # Insert corrupted ingredient with invalid negative percentage directly into DB
        conn.execute(
            "INSERT INTO recipes (id, name, version, updated_at, is_archived) "
            "VALUES ('bad-1', 'Corrupt Formula', 1, '', 0);"
        )
        conn.execute(
            "INSERT INTO ingredients (recipe_id, sort_order, name, percentage, cost_per_unit) "
            "VALUES ('bad-1', 0, 'Bad Ing', '-10.00', '1.00');"
        )
        # Must not throw; corrupt recipe is skipped while valid recipe is returned
        with self.assertLogs("tared.storage.sqlite_repo", level="WARNING") as cm:
            active = self.repo.list_recipes()
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0].id, self.recipe.id)
        self.assertTrue(any("Corrupt Formula" in msg for msg in cm.output))

    def test_catalog_ingredient_crud_and_conflict_update(self) -> None:
        sugar = CatalogIngredient(
            name="White Sugar",
            cost_per_unit=Decimal("1.25"),
            supplier="Bulk Refiners",
            sku="SUG-01",
            notes="Standard 25kg bags",
        )
        self.repo.save_catalog_ingredient(sugar)

        retrieved = self.repo.get_catalog_ingredient("white sugar")
        self.assertIsNotNone(retrieved)
        assert retrieved is not None
        self.assertEqual(retrieved.name, "White Sugar")
        self.assertEqual(retrieved.cost_per_unit, Decimal("1.25"))
        self.assertEqual(retrieved.supplier, "Bulk Refiners")

        sugar_updated = CatalogIngredient(
            name="WHITE SUGAR",
            cost_per_unit=Decimal("1.45"),
            supplier="Premium Sugars Ltd",
        )
        self.repo.save_catalog_ingredient(sugar_updated)

        catalog = self.repo.list_catalog_ingredients()
        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0].cost_per_unit, Decimal("1.45"))
        self.assertEqual(catalog[0].supplier, "Premium Sugars Ltd")

        deleted = self.repo.delete_catalog_ingredient(catalog[0].id)
        self.assertTrue(deleted)
        self.assertIsNone(self.repo.get_catalog_ingredient("White Sugar"))

    def test_record_batch_strips_batch_number_and_rejects_empty(self) -> None:
        self.repo.save_recipe(self.recipe)
        with self.assertRaises(ValueError):
            self.repo.record_batch(
                recipe_id=self.recipe.id,
                recipe_version=1,
                batch_number="   ",
                operator="Op",
                target_weight="100.00",
                total_weight="100.00",
                total_cost="1.00",
            )
        self.repo.record_batch(
            recipe_id=self.recipe.id,
            recipe_version=1,
            batch_number="  BAT-PADDED-01  ",
            operator="Op",
            target_weight="100.00",
            total_weight="100.00",
            total_cost="1.00",
        )
        self.assertTrue(self.repo.batch_exists("BAT-PADDED-01"))
        self.assertTrue(self.repo.batch_exists("  BAT-PADDED-01  "))


class TestJSONRepository(unittest.TestCase):
    def setUp(self) -> None:
        self.recipe = Recipe(
            id="json-uuid",
            name="JSON Test Formulation",
            version=4,
            updated_at="2026-11-06T09:30:00Z",
            ingredients=(Ingredient(name="Component A", percentage=Decimal("100.00")),),
        )

    def test_json_roundtrip_serialization_with_metadata(self) -> None:
        json_str = export_recipe_to_json(self.recipe)
        restored = import_recipe_from_json(json_str)

        self.assertEqual(restored.id, self.recipe.id)
        self.assertEqual(restored.name, self.recipe.name)
        self.assertEqual(restored.version, 4)
        self.assertEqual(restored.updated_at, "2026-11-06T09:30:00Z")

    def test_import_json_with_null_description_and_timestamp(self) -> None:
        raw_json = (
            '{"name": "Null Description Formula", "description": null, '
            '"updated_at": null, "ingredients": [{"name": "A", "percentage": "100.00"}]}'
        )
        recipe = import_recipe_from_json(raw_json)
        self.assertEqual(recipe.description, "")
        self.assertEqual(recipe.updated_at, "")

    def test_import_missing_id_generates_unique_uuid(self) -> None:
        raw_json1 = '{"name": "Batch 1", "ingredients": [{"name": "A", "percentage": "100.00"}]}'
        raw_json2 = '{"name": "Batch 2", "ingredients": [{"name": "A", "percentage": "100.00"}]}'

        r1 = import_recipe_from_json(raw_json1)
        r2 = import_recipe_from_json(raw_json2)

        self.assertTrue(len(r1.id) > 0)
        self.assertTrue(len(r2.id) > 0)
        self.assertNotEqual(r1.id, r2.id)


if __name__ == "__main__":
    unittest.main()
