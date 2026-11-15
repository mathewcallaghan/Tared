"""Headless unit tests verifying RecipeService formulation change detection, versioning, and import rules."""

import unittest
from decimal import Decimal

from tared.core.calculator import PercentageSumError
from tared.core.models import CatalogIngredient, Ingredient, Recipe
from tared.core.service import RecipeService, summarize_revision_diff
from tared.storage.sqlite_repo import SQLiteRecipeRepository


class TestRecipeService(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = SQLiteRecipeRepository(":memory:")
        self.service = RecipeService(self.repo)
        self.initial_recipe = Recipe(
            id="rec-001",
            name="Vanilla Glaze",
            description="Initial mixing instructions",
            version=1,
            ingredients=(
                Ingredient(name="Sugar", percentage=Decimal("70.00"), cost_per_unit=Decimal("0.65")),
                Ingredient(name="Water", percentage=Decimal("30.00"), cost_per_unit=Decimal("0.05")),
            ),
        )

    def tearDown(self) -> None:
        self.repo.close()

    def test_new_recipe_defaults_to_version_1(self) -> None:
        saved = self.service.save_recipe(self.initial_recipe)
        self.assertEqual(saved.version, 1)

    def test_price_change_increments_recipe_version(self) -> None:
        self.service.save_recipe(self.initial_recipe)

        updated_price = Recipe(
            id=self.initial_recipe.id,
            name=self.initial_recipe.name,
            description=self.initial_recipe.description,
            ingredients=(
                Ingredient(name="Sugar", percentage=Decimal("70.00"), cost_per_unit=Decimal("0.85")),
                Ingredient(name="Water", percentage=Decimal("30.00"), cost_per_unit=Decimal("0.05")),
            ),
        )
        saved = self.service.save_recipe(updated_price)
        self.assertEqual(saved.version, 2)
        self.assertEqual(saved.ingredients[0].cost_per_unit, Decimal("0.85"))

    def test_description_change_increments_recipe_version(self) -> None:
        self.service.save_recipe(self.initial_recipe)

        updated_desc = Recipe(
            id=self.initial_recipe.id,
            name=self.initial_recipe.name,
            description="Mix water at 60C before adding sugar",
            ingredients=self.initial_recipe.ingredients,
        )
        saved = self.service.save_recipe(updated_desc)
        self.assertEqual(saved.version, 2)
        self.assertEqual(saved.description, "Mix water at 60C before adding sugar")

    def test_identical_recipe_preserves_version(self) -> None:
        self.service.save_recipe(self.initial_recipe)

        identical = Recipe(
            id=self.initial_recipe.id,
            name=self.initial_recipe.name,
            description=self.initial_recipe.description,
            ingredients=self.initial_recipe.ingredients,
        )
        saved = self.service.save_recipe(identical)
        self.assertEqual(saved.version, 1)

    def test_percentage_change_increments_version(self) -> None:
        self.service.save_recipe(self.initial_recipe)

        formula_change = Recipe(
            id=self.initial_recipe.id,
            name=self.initial_recipe.name,
            ingredients=(
                Ingredient(name="Sugar", percentage=Decimal("65.00"), cost_per_unit=Decimal("0.65")),
                Ingredient(name="Water", percentage=Decimal("35.00"), cost_per_unit=Decimal("0.05")),
            ),
        )
        saved = self.service.save_recipe(formula_change)
        self.assertEqual(saved.version, 2)

    def test_ingredient_reordering_increments_version(self) -> None:
        self.service.save_recipe(self.initial_recipe)

        reordered = Recipe(
            id=self.initial_recipe.id,
            name=self.initial_recipe.name,
            ingredients=(
                Ingredient(name="Water", percentage=Decimal("30.00"), cost_per_unit=Decimal("0.05")),
                Ingredient(name="Sugar", percentage=Decimal("70.00"), cost_per_unit=Decimal("0.65")),
            ),
        )
        saved = self.service.save_recipe(reordered)
        self.assertEqual(saved.version, 2)

    def test_ingredient_addition_and_removal_increments_version(self) -> None:
        self.service.save_recipe(self.initial_recipe)

        added = Recipe(
            id=self.initial_recipe.id,
            name=self.initial_recipe.name,
            ingredients=(
                Ingredient(name="Sugar", percentage=Decimal("65.00"), cost_per_unit=Decimal("0.65")),
                Ingredient(name="Water", percentage=Decimal("30.00"), cost_per_unit=Decimal("0.05")),
                Ingredient(name="Vanilla Extract", percentage=Decimal("5.00"), cost_per_unit=Decimal("12.00")),
            ),
        )
        saved = self.service.save_recipe(added)
        self.assertEqual(saved.version, 2)

    def test_recipe_rename_increments_version(self) -> None:
        self.service.save_recipe(self.initial_recipe)

        renamed = Recipe(
            id=self.initial_recipe.id,
            name="Vanilla Glaze Standard",
            ingredients=self.initial_recipe.ingredients,
        )
        saved = self.service.save_recipe(renamed)
        self.assertEqual(saved.version, 2)

    def test_save_ingredient_to_catalog_status_and_conflict(self) -> None:
        # 1. Newly added item
        status, old_price = self.service.save_ingredient_to_catalog("Guar Gum", Decimal("14.00"))
        self.assertEqual(status, "added")
        self.assertIsNone(old_price)

        # 2. Identical item re-save
        status, old_price = self.service.save_ingredient_to_catalog("guar gum", Decimal("14.00"))
        self.assertEqual(status, "identical")
        self.assertEqual(old_price, Decimal("14.00"))

        # 3. Different price detection
        status, old_price = self.service.save_ingredient_to_catalog("Guar Gum", Decimal("16.50"))
        self.assertEqual(status, "different")
        self.assertEqual(old_price, Decimal("14.00"))

    def test_service_sync_recipe_costs_from_catalog(self) -> None:
        # Seed catalog with new market prices
        self.repo.save_catalog_ingredient(CatalogIngredient("Sugar", Decimal("1.10")))
        self.repo.save_catalog_ingredient(CatalogIngredient("Water", Decimal("0.08")))

        recipe = Recipe(
            id="rec-sync-01",
            name="Syrup",
            ingredients=(
                Ingredient("Sugar", Decimal("70.00"), Decimal("0.65")),
                Ingredient("Water", Decimal("30.00"), Decimal("0.08")),  # already matches
            ),
        )

        synced_recipe, changes = self.service.sync_recipe_costs_from_catalog(recipe)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0], ("Sugar", Decimal("0.65"), Decimal("1.10")))
        self.assertEqual(synced_recipe.ingredients[0].cost_per_unit, Decimal("1.10"))
        self.assertEqual(synced_recipe.ingredients[1].cost_per_unit, Decimal("0.08"))

    def test_service_update_catalog_from_recipe(self) -> None:
        recipe = Recipe(
            id="rec-learn-01",
            name="Compound",
            ingredients=(
                Ingredient("Pectin", Decimal("5.00"), Decimal("18.50")),
                Ingredient("Citric Acid", Decimal("95.00"), Decimal("4.20")),
            ),
        )
        count = self.service.update_catalog_from_recipe(recipe)
        self.assertEqual(count, 2)

        self.assertEqual(self.service.get_catalog_price("Pectin"), Decimal("18.50"))
        self.assertEqual(self.service.get_catalog_price("citric acid"), Decimal("4.20"))

    def test_service_update_catalog_skips_zero_cost_and_preserves_metadata(self) -> None:
        self.repo.save_catalog_ingredient(
            CatalogIngredient(
                name="Vanilla Extract",
                cost_per_unit=Decimal("45.00"),
                supplier="Flavor Corp",
                sku="VAN-01",
            )
        )
        recipe = Recipe(
            id="rec-zero-learn",
            name="Vanilla Recipe With Default Zero Price",
            ingredients=(
                Ingredient("Vanilla Extract", Decimal("10.00"), Decimal("0.00")),
                Ingredient("Sugar", Decimal("90.00"), Decimal("1.20")),
            ),
        )

        diffs = self.service.preview_learn_from_recipe(recipe)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0][0], "Sugar")

        count = self.service.update_catalog_from_recipe(recipe)
        self.assertEqual(count, 1)

        vanilla = self.repo.get_catalog_ingredient("Vanilla Extract")
        self.assertIsNotNone(vanilla)
        assert vanilla is not None
        self.assertEqual(vanilla.cost_per_unit, Decimal("45.00"))
        self.assertEqual(vanilla.supplier, "Flavor Corp")
        self.assertEqual(vanilla.sku, "VAN-01")

    def test_import_fresh_recipe_starts_at_version_1(self) -> None:
        foreign_recipe = Recipe(
            id="foreign-uuid-001",
            name="External Formula",
            version=5,
            ingredients=(Ingredient(name="Component A", percentage=Decimal("100.00")),),
        )
        imported = self.service.import_recipe(foreign_recipe)
        self.assertEqual(imported.id, "foreign-uuid-001")
        self.assertEqual(imported.version, 1)

        revisions = self.repo.get_recipe_revisions(imported.id)
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["version"], 1)

    def test_import_collision_copy_mode_assigns_new_id(self) -> None:
        self.service.save_recipe(self.initial_recipe)

        incoming = Recipe(
            id=self.initial_recipe.id,
            name="Incoming Variant",
            ingredients=(Ingredient(name="Base", percentage=Decimal("100.00")),),
        )
        imported = self.service.import_recipe(incoming, collision_mode="copy")

        self.assertNotEqual(imported.id, self.initial_recipe.id)
        self.assertIn("Imported Copy", imported.name)
        self.assertEqual(imported.version, 1)

        # Original recipe remains untouched
        original = self.repo.get_recipe(self.initial_recipe.id)
        assert original is not None
        self.assertEqual(original.name, "Vanilla Glaze")

    def test_import_collision_overwrite_mode_bumps_version(self) -> None:
        self.service.save_recipe(self.initial_recipe)

        incoming = Recipe(
            id=self.initial_recipe.id,
            name="Overwritten Formula",
            version=3,
            ingredients=(Ingredient(name="Base", percentage=Decimal("100.00")),),
        )
        imported = self.service.import_recipe(incoming, collision_mode="overwrite")

        self.assertEqual(imported.id, self.initial_recipe.id)
        self.assertEqual(imported.name, "Overwritten Formula")
        self.assertEqual(imported.version, 4)

        revisions = self.repo.get_recipe_revisions(self.initial_recipe.id)
        self.assertEqual(len(revisions), 2)
        self.assertEqual(revisions[0]["version"], 4)
        self.assertEqual(revisions[1]["version"], 1)

    def test_import_collision_raise_mode(self) -> None:
        self.service.save_recipe(self.initial_recipe)

        incoming = Recipe(
            id=self.initial_recipe.id,
            name="Duplicate",
            ingredients=self.initial_recipe.ingredients,
        )
        with self.assertRaises(ValueError):
            self.service.import_recipe(incoming, collision_mode="raise")

    def test_summarize_revision_diff_detects_all_change_types(self) -> None:
        self.service.save_recipe(self.initial_recipe)

        v2 = Recipe(
            id=self.initial_recipe.id,
            name="Vanilla Glaze Pro",
            description="Updated SOP",
            ingredients=(
                Ingredient(name="Sugar", percentage=Decimal("65.00"), cost_per_unit=Decimal("0.85")),
                Ingredient(name="Water", percentage=Decimal("30.00"), cost_per_unit=Decimal("0.05")),
                Ingredient(name="Vanilla", percentage=Decimal("5.00"), cost_per_unit=Decimal("12.00")),
            ),
        )
        self.service.save_recipe(v2)

        v3 = Recipe(
            id=self.initial_recipe.id,
            name="Vanilla Glaze Pro",
            description="Updated SOP",
            ingredients=(
                Ingredient(name="Water", percentage=Decimal("30.00"), cost_per_unit=Decimal("0.05")),
                Ingredient(name="Sugar", percentage=Decimal("70.00"), cost_per_unit=Decimal("0.95")),
            ),
        )
        self.service.save_recipe(v3)

        revs = self.repo.get_recipe_revisions(self.initial_recipe.id)
        self.assertEqual(len(revs), 3)
        self.assertEqual(summarize_revision_diff(None, revs[2]), "Initial saved formulation")

        diff_v2 = summarize_revision_diff(revs[2], revs[1])
        self.assertIn("Renamed to 'Vanilla Glaze Pro'", diff_v2)
        self.assertIn("Updated description/SOP", diff_v2)
        self.assertIn("Sugar: 70.00% → 65.00%", diff_v2)
        self.assertIn("+2 more", diff_v2)

        diff_v3 = summarize_revision_diff(revs[1], revs[0])
        self.assertIn("Removed Vanilla", diff_v3)
        self.assertIn("Sugar: 65.00% → 70.00%", diff_v3)
        self.assertIn("Sugar cost: $0.85 → $0.95/kg", diff_v3)

        v4 = Recipe(
            id=self.initial_recipe.id,
            name="Vanilla Glaze Pro",
            description="Updated SOP",
            ingredients=(
                Ingredient(name="Sugar", percentage=Decimal("70.00"), cost_per_unit=Decimal("0.95")),
                Ingredient(name="Water", percentage=Decimal("30.00"), cost_per_unit=Decimal("0.05")),
            ),
        )
        self.service.save_recipe(v4)
        revs_v4 = self.repo.get_recipe_revisions(self.initial_recipe.id)
        self.assertEqual(summarize_revision_diff(revs_v4[1], revs_v4[0]), "Reordered ingredients")
        self.assertIn("Added Vanilla (5.00%)", summarize_revision_diff(revs_v4[0], revs_v4[2]))

    def test_draft_lifecycle_saves_without_bumping_and_finalizes(self) -> None:
        draft_v1 = Recipe(
            id="rec-draft-01",
            name="R&D Formula",
            is_draft=True,
            ingredients=(
                Ingredient("Water", Decimal("50.00"), Decimal("0.10")),
                Ingredient("Sugar", Decimal("30.00"), Decimal("1.00")),
            ),
        )
        saved1 = self.service.save_recipe(draft_v1)
        self.assertEqual(saved1.version, 1)
        self.assertTrue(saved1.is_draft)
        self.assertEqual(len(self.repo.get_recipe_revisions("rec-draft-01")), 0)

        # Repeated edits in draft mode stay at v1 with zero revisions
        draft_edit2 = Recipe(
            id="rec-draft-01",
            name="R&D Formula vNext",
            description="Updated mixing",
            is_draft=True,
            ingredients=(
                Ingredient("Water", Decimal("70.00"), Decimal("0.10")),
                Ingredient("Sugar", Decimal("30.00"), Decimal("1.20")),
            ),
        )
        saved2 = self.service.save_recipe(draft_edit2)
        self.assertEqual(saved2.version, 1)
        self.assertTrue(saved2.is_draft)
        self.assertEqual(len(self.repo.get_recipe_revisions("rec-draft-01")), 0)

        # Finalize v1 draft -> locks v1 into recipe_revisions
        finalize_v1 = Recipe(
            id=saved2.id,
            name=saved2.name,
            description=saved2.description,
            is_draft=False,
            ingredients=saved2.ingredients,
        )
        finalized = self.service.save_recipe(finalize_v1)
        self.assertEqual(finalized.version, 1)
        self.assertFalse(finalized.is_draft)
        self.assertEqual(len(self.repo.get_recipe_revisions("rec-draft-01")), 1)

        # Branch v2 draft from released v1 -> reserves v2 while preserving v1 revision
        branch_v2 = Recipe(
            id="rec-draft-01",
            name=finalized.name,
            description="Testing v2 changes",
            is_draft=True,
            ingredients=finalized.ingredients,
        )
        saved_v2_draft = self.service.save_recipe(branch_v2)
        self.assertEqual(saved_v2_draft.version, 2)
        self.assertTrue(saved_v2_draft.is_draft)
        self.assertEqual(len(self.repo.get_recipe_revisions("rec-draft-01")), 1)

        # Discard v2 draft -> reverts cleanly back to released v1
        reverted = self.service.discard_draft("rec-draft-01")
        self.assertIsNotNone(reverted)
        assert reverted is not None
        self.assertEqual(reverted.version, 1)
        self.assertFalse(reverted.is_draft)
        self.assertEqual(reverted.description, "Updated mixing")

    def test_save_non_draft_incomplete_percentages_raises_error(self) -> None:
        incomplete = Recipe(
            id="rec-incomplete-final",
            name="Incomplete Final",
            is_draft=False,
            ingredients=(
                Ingredient("Water", Decimal("50.00")),
                Ingredient("Sugar", Decimal("30.00")),
            ),
        )
        with self.assertRaises(PercentageSumError):
            self.service.save_recipe(incomplete)

    def test_import_non_draft_incomplete_percentages_raises_error(self) -> None:
        incomplete = Recipe(
            id="rec-import-incomplete",
            name="Invalid Imported Final",
            is_draft=False,
            ingredients=(
                Ingredient("Water", Decimal("50.00")),
                Ingredient("Sugar", Decimal("30.00")),
            ),
        )
        with self.assertRaises(PercentageSumError):
            self.service.import_recipe(incomplete)
        self.assertIsNone(self.repo.get_recipe("rec-import-incomplete"))

    def test_import_draft_incomplete_percentages_succeeds(self) -> None:
        draft_import = Recipe(
            id="rec-import-draft",
            name="Valid Imported Draft",
            is_draft=True,
            ingredients=(
                Ingredient("Water", Decimal("60.00")),
                Ingredient("Sugar", Decimal("20.00")),
            ),
        )
        imported = self.service.import_recipe(draft_import)
        self.assertTrue(imported.is_draft)
        self.assertEqual(imported.version, 1)
        self.assertEqual(len(self.repo.get_recipe_revisions("rec-import-draft")), 0)


    def test_import_recipe_detects_archived_corrupted_recipe_collision(self) -> None:
        conn = self.repo._get_connection()
        conn.execute(
            "INSERT INTO recipes (id, name, version, updated_at, is_archived) "
            "VALUES ('corrupt-arch-imp', 'Corrupt Archived Formula', 1, '', 1);"
        )

        incoming = Recipe(
            id="corrupt-arch-imp",
            name="Restored Replacement",
            version=1,
            ingredients=(Ingredient("Water", Decimal("100.00")),),
        )
        imported = self.service.import_recipe(incoming, collision_mode="overwrite")
        self.assertEqual(imported.id, "corrupt-arch-imp")
        self.assertEqual(imported.name, "Restored Replacement")

    def test_summarize_revision_diff_reports_casing_only_ingredient_rename(self) -> None:
        r_v1 = Recipe(
            id="rec-case-rename",
            name="Case Test",
            ingredients=(Ingredient("water", Decimal("100.00"), Decimal("0.10")),),
        )
        self.service.save_recipe(r_v1)
        r_v2 = Recipe(
            id="rec-case-rename",
            name="Case Test",
            ingredients=(Ingredient("Water", Decimal("100.00"), Decimal("0.10")),),
        )
        saved_v2 = self.service.save_recipe(r_v2)
        self.assertEqual(saved_v2.version, 2)

        revs = self.repo.get_recipe_revisions("rec-case-rename")
        diff = summarize_revision_diff(revs[1], revs[0])
        self.assertIn("Renamed 'water' → 'Water'", diff)

    def test_ingredient_strips_whitespace_and_avoids_spurious_diff(self) -> None:
        from tared.core.service import has_formulation_changed

        padded = Ingredient(name="  Water  ", percentage=Decimal("100.00"), cost_per_unit=Decimal("0.10"))
        self.assertEqual(padded.name, "Water")

        r1 = Recipe(id="rec-ws-1", name="Base", ingredients=(padded,))
        r2 = Recipe(
            id="rec-ws-1",
            name="Base",
            ingredients=(Ingredient(name="Water", percentage=Decimal("100.00"), cost_per_unit=Decimal("0.10")),),
        )
        self.assertFalse(has_formulation_changed(r1, r2))

    def test_recipe_strips_name_and_id_and_handles_corrupt_revision_or_null_diff_cost(self) -> None:
        padded = Recipe(
            id="  rec-padded-01  ",
            name="  Padded Formula  ",
            ingredients=(Ingredient("Water", Decimal("100.00"), Decimal("0.10")),),
        )
        self.assertEqual(padded.id, "rec-padded-01")
        self.assertEqual(padded.name, "Padded Formula")

        self.service.save_recipe(self.initial_recipe)
        with self.repo._get_connection() as conn:
            conn.execute("DROP TRIGGER IF EXISTS prevent_revision_update;")
            conn.execute(
                "UPDATE recipe_revisions SET ingredients_json = ? WHERE recipe_id = ?;",
                ('[{"name": "Sugar", "percentage": "bad", "cost_per_unit": "1.00"}]', self.initial_recipe.id),
            )
        reloaded = self.repo.get_recipe(self.initial_recipe.id)
        assert reloaded is not None
        self.assertEqual(self.service.compute_next_version(reloaded), 1)

        prev_json = '[{"name": "Water", "percentage": "100.00", "cost_per_unit": null}]'
        curr_json = (
            '[{"name": "Water", "percentage": "90.00", "cost_per_unit": null}, '
            '{"name": "Salt", "percentage": "10.00", "cost_per_unit": null}]'
        )
        diff_summary = summarize_revision_diff(
            {"name": "F", "description": "", "ingredients_json": prev_json},
            {"name": "F", "description": "", "ingredients_json": curr_json},
        )
        self.assertIn("Water: 100.00% → 90.00%", diff_summary)
        self.assertIn("Added Salt (10.00%)", diff_summary)

    def test_discard_draft_rejects_archived_draft(self) -> None:
        archived_draft = Recipe(
            id="rec-arch-draft",
            name="Archived Draft",
            version=1,
            is_draft=True,
            ingredients=(Ingredient("Water", Decimal("50.00")),),
        )
        self.service.save_recipe(archived_draft)
        self.repo.delete_recipe("rec-arch-draft")
        with self.assertRaises(ValueError):
            self.service.discard_draft("rec-arch-draft")


if __name__ == "__main__":
    unittest.main()
