"""Unit and integration tests for weight-based formulation, discrete mass conservation, and dialogs."""

import os
import random
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC_DIR = Path(__file__).resolve().parents[2] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

try:
    from PyQt6.QtWidgets import QApplication, QMessageBox

    from tared.core.calculator import compute_batch
    from tared.core.models import CatalogIngredient, Ingredient, Recipe
    from tared.storage.sqlite_repo import SQLiteRecipeRepository
    from tared.ui.models.recipe_table_model import RecipeTableModel, _parse_decimal_input
    from tared.ui.views.main_window import MainWindow
    from tared.ui.views.weight_entry_dialog import BatchByWeightDialog, _format_unit_cost

    qt_available = True
except ImportError:
    qt_available = False


class TestWeightBasedFormulation(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not qt_available:
            return
        if QApplication.instance() is None:
            cls.app = QApplication(["test-weight-runner"])
        else:
            cls.app = QApplication.instance()

    def setUp(self) -> None:
        if not qt_available:
            self.skipTest("PyQt6 not available.")

        self.question_patcher = patch(
            "PyQt6.QtWidgets.QMessageBox.question", return_value=QMessageBox.StandardButton.Discard
        )
        self.mock_question = self.question_patcher.start()
        self.addCleanup(self.question_patcher.stop)

        self.warn_patcher = patch(
            "PyQt6.QtWidgets.QMessageBox.warning", return_value=QMessageBox.StandardButton.Ok
        )
        self.mock_warn = self.warn_patcher.start()
        self.addCleanup(self.warn_patcher.stop)

        self.info_patcher = patch(
            "PyQt6.QtWidgets.QMessageBox.information", return_value=QMessageBox.StandardButton.Ok
        )
        self.mock_info = self.info_patcher.start()
        self.addCleanup(self.info_patcher.stop)

        self.crit_patcher = patch(
            "PyQt6.QtWidgets.QMessageBox.critical", return_value=QMessageBox.StandardButton.Ok
        )
        self.mock_crit = self.crit_patcher.start()
        self.addCleanup(self.crit_patcher.stop)

        self.model = RecipeTableModel()

    def test_format_unit_cost_whole_numbers(self) -> None:
        self.assertEqual(_format_unit_cost(Decimal("20")), "20.00")
        self.assertEqual(_format_unit_cost(Decimal("100")), "100.00")
        self.assertEqual(_format_unit_cost(Decimal("120")), "120.00")
        self.assertEqual(_format_unit_cost(Decimal("1.5")), "1.50")
        self.assertEqual(_format_unit_cost(Decimal("0.0035")), "0.0035")
        self.assertEqual(_format_unit_cost(Decimal("12.3456")), "12.3456")

    def test_parse_decimal_input_handles_mass_unit_suffixes(self) -> None:
        self.assertEqual(_parse_decimal_input("3500g", convert_kg=True, allow_mass_units=True), Decimal("3500"))
        self.assertEqual(_parse_decimal_input("3500 g", convert_kg=True, allow_mass_units=True), Decimal("3500"))
        self.assertEqual(_parse_decimal_input("14.5 G", convert_kg=True, allow_mass_units=True), Decimal("14.5"))
        self.assertEqual(_parse_decimal_input("3.5kg", convert_kg=True, allow_mass_units=True), Decimal("3500"))
        self.assertEqual(_parse_decimal_input("3.52kg", convert_kg=True, allow_mass_units=True), Decimal("3520"))
        self.assertEqual(_parse_decimal_input("0.25 KG", convert_kg=True, allow_mass_units=True), Decimal("250"))
        self.assertEqual(_parse_decimal_input("1.5Kg", convert_kg=True, allow_mass_units=True), Decimal("1500"))
        self.assertEqual(_parse_decimal_input("120.00 %"), Decimal("120.00"))
        self.assertEqual(_parse_decimal_input("$45.00"), Decimal("45.00"))

    def test_parse_decimal_input_rejects_units_when_disallowed(self) -> None:
        with self.assertRaises(ValueError):
            _parse_decimal_input("12kg", convert_kg=False, allow_mass_units=False)
        with self.assertRaises(ValueError):
            _parse_decimal_input("50g", convert_kg=False, allow_mass_units=False)
        self.assertEqual(_parse_decimal_input("12kg", convert_kg=False, allow_mass_units=True), Decimal("12"))
        self.assertEqual(_parse_decimal_input("12kg", convert_kg=True), Decimal("12000"))

    def test_randomized_weight_to_percentage_batch_scaling_invariance(self) -> None:
        rnd = random.Random(101)
        precision = Decimal("0.01")

        for _ in range(30):
            num_components = rnd.randint(2, 8)
            weights = [Decimal(rnd.randint(1, 5000)) * precision for _ in range(num_components)]
            target_mass = sum(weights)

            items_payload = [
                {"name": f"Ing_{i}", "weight": w, "cost_per_unit": Decimal("1.00")}
                for i, w in enumerate(weights)
            ]

            self.model.set_ingredients_from_weights(items_payload, target_weight=target_mass)
            recipe = self.model.get_recipe()
            assert recipe is not None

            batch = compute_batch(recipe, target_weight=target_mass, precision=precision)
            self.assertEqual(batch.total_weight, target_mass)
            for idx, w in enumerate(weights):
                self.assertEqual(batch.items[idx].scaled_weight, w)

    def test_in_table_weight_input_populates_percentages_and_conserves_mass(self) -> None:
        self.model.set_target_weight(Decimal("3520.00"))

        self.model.add_ingredient("Apple Juice", Decimal("0.00"), Decimal("1.20"))
        self.model.add_ingredient("Yeast", Decimal("0.00"), Decimal("25.00"))
        self.model.add_ingredient("DAP", Decimal("0.00"), Decimal("15.00"))

        success1 = self.model.setData(self.model.index(0, 3), "3500")
        self.assertTrue(success1)
        success2 = self.model.setData(self.model.index(1, 3), "14g")
        self.assertTrue(success2)
        success3 = self.model.setData(self.model.index(2, 3), "6.00")
        self.assertTrue(success3)

        items = self.model.ingredients_data
        total_pct = sum(it["percentage"] for it in items)
        self.assertAlmostEqual(float(total_pct), 100.0, places=4)

        self.assertIsNotNone(self.model.current_batch)
        batch = self.model.current_batch
        assert batch is not None
        self.assertEqual(batch.total_weight, Decimal("3520.00"))
        self.assertEqual(batch.items[0].scaled_weight, Decimal("3500.00"))
        self.assertEqual(batch.items[1].scaled_weight, Decimal("14.00"))
        self.assertEqual(batch.items[2].scaled_weight, Decimal("6.00"))
        self.assertEqual(batch.items[0].cumulative_tare, Decimal("3500.00"))
        self.assertEqual(batch.items[1].cumulative_tare, Decimal("3514.00"))
        self.assertEqual(batch.items[2].cumulative_tare, Decimal("3520.00"))

    def test_in_table_weight_exceeding_target_mass_is_rejected(self) -> None:
        self.model.set_target_weight(Decimal("1000.00"))
        self.model.add_ingredient("Sugar", Decimal("0.00"))

        rejected_msgs = []
        self.model.edit_rejected.connect(lambda msg: rejected_msgs.append(msg))

        success = self.model.setData(self.model.index(0, 3), "1500")
        self.assertFalse(success)
        self.assertEqual(len(rejected_msgs), 1)
        self.assertIn("exceeds Target Batch Mass", rejected_msgs[0])

        success_neg = self.model.setData(self.model.index(0, 3), "-10")
        self.assertFalse(success_neg)

    def test_in_table_weight_non_multiple_of_precision_rejected(self) -> None:
        self.model.set_target_weight(Decimal("1000.00"))
        self.model.set_precision(Decimal("0.01"))
        self.model.add_ingredient("Component", Decimal("0.00"))

        rejected_msgs = []
        self.model.edit_rejected.connect(lambda msg: rejected_msgs.append(msg))

        success = self.model.setData(self.model.index(0, 3), "12.345")
        self.assertFalse(success)
        self.assertEqual(len(rejected_msgs), 1)
        self.assertIn("exact multiple of scale precision", rejected_msgs[0])

    def test_set_ingredients_from_weights_derives_exact_percentages(self) -> None:
        raw_items = [
            {"name": "Apple Juice", "weight": Decimal("3500.00"), "cost_per_unit": Decimal("1.20")},
            {"name": "Yeast", "weight": Decimal("14.00"), "cost_per_unit": Decimal("30.00")},
            {"name": "DAP", "weight": Decimal("6.00"), "cost_per_unit": Decimal("12.00")},
        ]
        self.model.set_ingredients_from_weights(raw_items, target_weight=Decimal("3520.00"))

        self.assertEqual(self.model.rowCount(), 3)
        self.assertIsNotNone(self.model.current_batch)
        batch = self.model.current_batch
        assert batch is not None
        self.assertEqual(batch.total_weight, Decimal("3520.00"))
        self.assertEqual(batch.items[0].scaled_weight, Decimal("3500.00"))
        self.assertEqual(batch.items[1].scaled_weight, Decimal("14.00"))
        self.assertEqual(batch.items[2].scaled_weight, Decimal("6.00"))

    def test_batch_by_weight_dialog_calculation_and_apply(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_catalog_ingredient(CatalogIngredient("Apple Juice", Decimal("1.50")))

        window = MainWindow(repo)
        self.addCleanup(window.close)

        dialog = BatchByWeightDialog(
            repository=repo,
            service=window.service,
            current_ingredients=[],
            current_target_weight=Decimal("1000.00"),
            parent=window,
        )

        dialog.table.setRowCount(0)
        dialog._add_row(name="Apple Juice", weight=Decimal("3500.00"))
        dialog._add_row(name="Yeast", weight=Decimal("14.00"), cost=Decimal("40.00"))
        dialog._add_row(name="DAP", weight=Decimal("6.00"), cost=Decimal("15.00"))

        self.assertEqual(dialog.table.item(0, 2).text(), "1.50")
        self.assertEqual(dialog.get_target_weight(), Decimal("3520.00"))
        self.assertIn("3520.00 g", dialog.summary_label.text())
        self.assertTrue(dialog.btn_apply.isEnabled())

        ingredients = dialog.get_ingredients()
        self.assertEqual(len(ingredients), 3)
        self.assertEqual(ingredients[0]["weight"], Decimal("3500.00"))
        self.assertEqual(ingredients[1]["weight"], Decimal("14.00"))
        self.assertEqual(ingredients[2]["weight"], Decimal("6.00"))

        dialog.close()

    def test_dialog_rejects_invalid_unit_cost_in_apply(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        dialog = BatchByWeightDialog(
            repository=repo,
            service=None,
            current_ingredients=[],
            current_target_weight=Decimal("1000.00"),
            precision=Decimal("0.01"),
        )
        dialog.table.setRowCount(0)
        dialog._add_row("Component A", Decimal("100.00"), Decimal("1.00"))

        dialog.table.item(0, 2).setText("12kg")

        warn_called = False

        def mock_warning(*args, **kwargs):
            nonlocal warn_called
            warn_called = True

        with patch("PyQt6.QtWidgets.QMessageBox.warning", side_effect=mock_warning):
            dialog._apply()

        self.assertTrue(warn_called)
        dialog.close()

    def test_main_window_formulate_by_weight_action(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        test_payload = [
            {"name": "Apple Juice", "weight": Decimal("3500.00"), "cost_per_unit": Decimal("1.20")},
            {"name": "Yeast", "weight": Decimal("14.00"), "cost_per_unit": Decimal("25.00")},
            {"name": "DAP", "weight": Decimal("6.00"), "cost_per_unit": Decimal("15.00")},
        ]
        target_mass = Decimal("3520.00")

        window.target_weight_spin.setValue(float(target_mass))
        window.table_model.set_ingredients_from_weights(test_payload, target_weight=target_mass)
        window._set_dirty(True)

        self.assertTrue(window._is_dirty)
        self.assertEqual(window.table_model.rowCount(), 3)
        self.assertIn("100.00% (Valid)", window.validation_label.text())
        self.assertIn("3520.00 g", window.summary_label.text())

        window._set_dirty(False)

    def test_reopen_dialog_retains_exact_formula_and_target_mass(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        recipe = Recipe(
            id="third-test",
            name="Thirds Formula",
            version=1,
            ingredients=(
                Ingredient("A", Decimal("33.3333"), Decimal("1.00")),
                Ingredient("B", Decimal("33.3333"), Decimal("1.00")),
                Ingredient("C", Decimal("33.3334"), Decimal("1.00")),
            ),
        )
        repo.save_recipe(recipe)

        window = MainWindow(repo)
        self.addCleanup(window.close)
        window.target_weight_spin.setValue(1000.00)

        self.assertEqual(window.name_input.text(), "Thirds Formula")
        self.assertFalse(window._is_dirty)

        dialog = BatchByWeightDialog(
            repository=repo,
            service=window.service,
            current_ingredients=window.table_model.ingredients_data,
            current_target_weight=Decimal("1000.00"),
            precision=Decimal("0.01"),
            parent=window,
        )

        self.assertEqual(dialog.get_target_weight(), Decimal("1000.00"))
        self.assertFalse(dialog.has_user_edits())

        # Test apply branch when user makes a cost edit in the dialog
        dialog.table.item(0, 2).setText("15.00")
        self.assertTrue(dialog.has_user_edits())

        with patch("PyQt6.QtWidgets.QDialog.exec", return_value=BatchByWeightDialog.DialogCode.Accepted):
            with patch("tared.ui.views.weight_entry_dialog.BatchByWeightDialog", return_value=dialog):
                window._open_batch_by_weight_dialog()

        self.assertTrue(window._is_dirty)
        self.assertEqual(window.table_model.ingredients_data[0]["percentage"], Decimal("33.3333"))
        self.assertEqual(window.table_model.ingredients_data[1]["percentage"], Decimal("33.3333"))
        self.assertEqual(window.table_model.ingredients_data[2]["percentage"], Decimal("33.3334"))
        self.assertEqual(window.target_weight_spin.value(), 1000.00)

    def test_cost_edit_in_dialog_preserves_exact_percentages(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        recipe = Recipe(
            id="cost-edit-test",
            name="Thirds Cost Test",
            version=1,
            ingredients=(
                Ingredient("A", Decimal("33.3333"), Decimal("20.00")),
                Ingredient("B", Decimal("33.3333"), Decimal("100.00")),
                Ingredient("C", Decimal("33.3334"), Decimal("120.00")),
            ),
        )
        repo.save_recipe(recipe)

        window = MainWindow(repo)
        self.addCleanup(window.close)
        window.target_weight_spin.setValue(1000.00)

        dialog = BatchByWeightDialog(
            repository=repo,
            service=window.service,
            current_ingredients=window.table_model.ingredients_data,
            current_target_weight=Decimal("1000.00"),
            precision=Decimal("0.01"),
            parent=window,
        )

        self.assertEqual(dialog.table.item(0, 2).text(), "20.00")
        self.assertEqual(dialog.table.item(1, 2).text(), "100.00")
        self.assertEqual(dialog.table.item(2, 2).text(), "120.00")

        dialog.table.item(0, 2).setText("25.00")
        self.assertTrue(dialog.has_user_edits())
        self.assertFalse(dialog._weights_modified)

        ingredients = dialog.get_ingredients()
        self.assertEqual(ingredients[0]["percentage"], Decimal("33.3333"))
        self.assertEqual(ingredients[1]["percentage"], Decimal("33.3333"))
        self.assertEqual(ingredients[2]["percentage"], Decimal("33.3334"))
        self.assertEqual(ingredients[0]["cost_per_unit"], Decimal("25.00"))

        dialog.close()

    def test_reopen_dialog_preserves_subcent_unit_costs(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        dialog = BatchByWeightDialog(
            repository=repo,
            service=None,
            current_ingredients=[
                {"name": "Trace Mineral", "weight": Decimal("10.00"), "cost_per_unit": Decimal("0.0035")},
                {"name": "Specialty Oil", "weight": Decimal("990.00"), "cost_per_unit": Decimal("12.3456")},
            ],
            current_target_weight=Decimal("1000.00"),
            precision=Decimal("0.01"),
        )

        self.assertEqual(dialog.table.item(0, 2).text(), "0.0035")
        self.assertEqual(dialog.table.item(1, 2).text(), "12.3456")

        ingredients = dialog.get_ingredients()
        self.assertEqual(ingredients[0]["cost_per_unit"], Decimal("0.0035"))
        self.assertEqual(ingredients[1]["cost_per_unit"], Decimal("12.3456"))
        dialog.close()

    def test_dialog_rejects_non_multiple_precision_weights(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        dialog = BatchByWeightDialog(
            repository=repo,
            service=None,
            current_ingredients=[],
            current_target_weight=Decimal("1000.00"),
            precision=Decimal("0.01"),
        )
        dialog.table.setRowCount(0)
        dialog._add_row(name="A", weight=Decimal("12.345"))

        self.assertFalse(dialog.btn_apply.isEnabled())
        self.assertIn("not a multiple of scale precision", dialog.summary_label.text())

        dialog.table.item(0, 1).setText("12.35")
        self.assertTrue(dialog.btn_apply.isEnabled())
        dialog.close()

    def test_percentage_editor_enter_does_not_truncate_exact_percentage(self) -> None:
        exact_pct = Decimal("100") / Decimal("3")
        self.model.add_ingredient("A", exact_pct)

        success = self.model.setData(self.model.index(0, 1), "33.333333")
        self.assertTrue(success)
        self.assertEqual(self.model.ingredients_data[0]["percentage"], exact_pct)

    def test_summary_label_dynamically_computes_formula_percentage(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        dialog = BatchByWeightDialog(
            repository=repo,
            service=None,
            current_ingredients=[
                {"name": "Apple", "weight": Decimal("500.00"), "cost_per_unit": Decimal("1.00")},
                {"name": "Water", "weight": Decimal("500.00"), "cost_per_unit": Decimal("0.00")},
            ],
            current_target_weight=Decimal("1000.00"),
            precision=Decimal("0.01"),
        )
        self.assertIn("Formula Total: 100.00%", dialog.summary_label.text())
        dialog.close()

    def test_proportional_scaling_mode_and_sub_step_weight(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        dialog = BatchByWeightDialog(
            repository=repo,
            service=None,
            current_ingredients=[],
            current_target_weight=Decimal("5000.00"),
            precision=Decimal("0.01"),
        )
        dialog.table.setRowCount(0)
        dialog.auto_target_cb.setChecked(False)
        dialog._add_row("Component", Decimal("0.004"))

        self.assertFalse(dialog.btn_apply.isEnabled())
        self.assertIn("smaller than scale precision", dialog.summary_label.text())

        dialog.table.item(0, 1).setText("3500.00")
        self.assertTrue(dialog.btn_apply.isEnabled())
        self.assertIn("Proportional Scaling", dialog.summary_label.text())
        dialog.close()


if __name__ == "__main__":
    unittest.main()
