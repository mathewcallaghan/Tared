"""Headless unit tests verifying RecipeService formulation change detection, versioning, and import rules."""

import unittest
from decimal import Decimal

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


if __name__ == "__main__":
    unittest.main()
