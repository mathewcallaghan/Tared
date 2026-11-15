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

    def test_reopen_dialog_with_zero_rounded_trace_ingredient_allows_apply(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        dialog = BatchByWeightDialog(
            repository=repo,
            service=None,
            current_ingredients=[
                {"name": "Micro Trace Active", "percentage": Decimal("0.001"), "cost_per_unit": Decimal("500.00")},
                {"name": "Bulk Excipient", "percentage": Decimal("99.999"), "cost_per_unit": Decimal("1.00")},
            ],
            current_target_weight=Decimal("10.00"),
            precision=Decimal("0.01"),
        )
        self.assertEqual(dialog.table.item(0, 1).text(), "0.00")
        self.assertTrue(dialog.btn_apply.isEnabled())

        dialog.table.item(0, 2).setText("550.00")
        with patch.object(dialog, "accept") as mock_accept:
            dialog._apply()
            mock_accept.assert_called_once()

        ingredients = dialog.get_ingredients()
        self.assertEqual(ingredients[0]["percentage"], Decimal("0.001"))
        self.assertEqual(ingredients[0]["cost_per_unit"], Decimal("550.00"))
        dialog.close()

    def test_dialog_spinbox_parses_en_us_and_de_de_grouped_target_weights(self) -> None:
        from PyQt6.QtCore import QLocale

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        dialog = BatchByWeightDialog(
            repository=repo,
            service=None,
            current_ingredients=[{"name": "Base", "weight": Decimal("500.00"), "cost_per_unit": Decimal("1.00")}],
            current_target_weight=Decimal("1000.00"),
            precision=Decimal("0.01"),
        )
        dialog.auto_target_cb.setChecked(False)

        dialog.target_weight_spin.setLocale(QLocale(QLocale.Language.English, QLocale.Country.UnitedStates))
        dialog.target_weight_spin.setValue(1250.50)
        dialog._recalculate()
        self.assertEqual(dialog._resolved_target_weight, Decimal("1250.50"))

        dialog.target_weight_spin.setLocale(QLocale(QLocale.Language.German, QLocale.Country.Germany))
        dialog.target_weight_spin.setValue(1250.50)
        dialog._recalculate()
        self.assertEqual(dialog._resolved_target_weight, Decimal("1250.50"))
        dialog.close()

    def test_reopen_dialog_preserves_intentional_zero_cost_when_catalog_has_price(self) -> None:
        from tared.core.service import RecipeService

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_catalog_ingredient(CatalogIngredient("Water", Decimal("0.50")))
        service = RecipeService(repo)

        dialog = BatchByWeightDialog(
            repository=repo,
            service=service,
            current_ingredients=[
                {"name": "Water", "percentage": Decimal("50.00"), "cost_per_unit": Decimal("0.00")},
                {"name": "Sugar", "percentage": Decimal("50.00"), "cost_per_unit": Decimal("1.00")},
            ],
            current_target_weight=Decimal("1000.00"),
            precision=Decimal("0.01"),
        )
        self.assertEqual(dialog.table.item(0, 2).text(), "0.00")
        ingredients = dialog.get_ingredients()
        self.assertEqual(ingredients[0]["cost_per_unit"], Decimal("0.00"))
        dialog.close()

    def test_invalid_and_negative_weight_surfaces_error_in_summary_and_disables_apply(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        dialog = BatchByWeightDialog(
            repository=repo,
            service=None,
            current_ingredients=[],
            current_target_weight=Decimal("1000.00"),
            precision=Decimal("0.01"),
        )
        self.addCleanup(dialog.close)
        dialog.table.setRowCount(0)
        dialog._add_row("Water", Decimal("100.00"))
        self.assertTrue(dialog.btn_apply.isEnabled())

        dialog.table.item(0, 1).setText("abc")
        self.assertFalse(dialog.btn_apply.isEnabled())
        self.assertIn("invalid weight", dialog.summary_label.text())

        dialog.table.item(0, 1).setText("-10")
        self.assertFalse(dialog.btn_apply.isEnabled())
        self.assertIn("cannot be negative", dialog.summary_label.text())

        dialog.table.item(0, 1).setText("100.00")
        self.assertTrue(dialog.btn_apply.isEnabled())

    def test_toggling_auto_target_preserves_trace_ingredient_and_exact_percentages(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        dialog = BatchByWeightDialog(
            repository=repo,
            service=None,
            current_ingredients=[
                {"name": "Micro Trace Active", "percentage": Decimal("0.001"), "cost_per_unit": Decimal("500.00")},
                {"name": "Bulk Excipient", "percentage": Decimal("99.999"), "cost_per_unit": Decimal("1.00")},
            ],
            current_target_weight=Decimal("10.00"),
            precision=Decimal("0.01"),
        )
        self.addCleanup(dialog.close)
        self.assertTrue(dialog.btn_apply.isEnabled())

        dialog.auto_target_cb.setChecked(False)
        dialog.target_weight_spin.setValue(50.00)
        self.assertFalse(dialog._weights_modified)
        self.assertTrue(dialog.has_user_edits())
        self.assertTrue(dialog.btn_apply.isEnabled())
        self.assertEqual(dialog.get_target_weight(), Decimal("50.00"))
        self.assertEqual(dialog.get_ingredients()[0]["percentage"], Decimal("0.001"))

    def test_reopen_incomplete_draft_keeps_initial_target_weight_unchecked(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        dialog = BatchByWeightDialog(
            repository=repo,
            service=None,
            current_ingredients=[
                {"name": "Water", "percentage": Decimal("25.00"), "cost_per_unit": Decimal("0.10")},
                {"name": "Sugar", "percentage": Decimal("25.00"), "cost_per_unit": Decimal("1.00")},
            ],
            current_target_weight=Decimal("1000.00"),
            precision=Decimal("0.01"),
        )
        self.addCleanup(dialog.close)
        self.assertFalse(dialog.auto_target_cb.isChecked())
        self.assertEqual(dialog.get_target_weight(), Decimal("1000.00"))
        self.assertIn("Target Mass: 1000.00 g", dialog.summary_label.text())
        self.assertIn("Formula Total: 50.00%", dialog.summary_label.text())

    def test_dialog_frames_avoid_hardcoded_light_backgrounds_without_text_color(self) -> None:
        from PyQt6.QtWidgets import QFrame

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        dialog = BatchByWeightDialog(
            repository=repo,
            service=None,
            current_ingredients=[],
            current_target_weight=Decimal("1000.00"),
            precision=Decimal("0.01"),
        )
        self.addCleanup(dialog.close)
        for frame in dialog.findChildren(QFrame):
            style = frame.styleSheet().lower()
            if "background-color" in style:
                self.assertIn("color:", style)

    def test_parse_decimal_input_space_grouping_localized_sci_and_bare_dot(self) -> None:
        from PyQt6.QtCore import QLocale

        fr_locale = QLocale(QLocale.Language.French, QLocale.Country.France)
        self.assertEqual(_parse_decimal_input("1 250,50", locale=fr_locale), Decimal("1250.50"))

        de_locale = QLocale(QLocale.Language.German, QLocale.Country.Germany)
        self.assertEqual(_parse_decimal_input("1,5e-4", locale=de_locale), Decimal("0.00015"))

        us_locale = QLocale(QLocale.Language.English, QLocale.Country.UnitedStates)
        with self.assertRaises(ValueError):
            _parse_decimal_input("1,5e-4", locale=us_locale)
        for bad in (".", "+.", "-."):
            with self.assertRaises(ValueError):
                _parse_decimal_input(bad, locale=us_locale)

    def test_checking_auto_target_on_incomplete_draft_normalizes_percentages(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        dialog = BatchByWeightDialog(
            repository=repo,
            service=None,
            current_ingredients=[
                {"name": "Water", "percentage": Decimal("25.00"), "cost_per_unit": Decimal("0.10")},
                {"name": "Sugar", "percentage": Decimal("25.00"), "cost_per_unit": Decimal("1.00")},
            ],
            current_target_weight=Decimal("1000.00"),
            precision=Decimal("0.01"),
        )
        self.addCleanup(dialog.close)
        self.assertFalse(dialog.auto_target_cb.isChecked())

        dialog.auto_target_cb.setChecked(True)
        self.assertEqual(dialog.get_target_weight(), Decimal("500.00"))
        self.assertIn("Formula Total: 100.00%", dialog.summary_label.text())
        applied = dialog.get_ingredients()
        self.assertNotIn("percentage", applied[0])
        self.assertEqual(applied[0]["weight"], Decimal("250.00"))

    def test_reverting_weight_typo_restores_trace_preservation_and_invalid_cost_shows_error(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        dialog = BatchByWeightDialog(
            repository=repo,
            service=None,
            current_ingredients=[
                {"name": "Micro Trace Active", "percentage": Decimal("0.001"), "cost_per_unit": Decimal("500.00")},
                {"name": "Bulk Excipient", "percentage": Decimal("99.999"), "cost_per_unit": Decimal("1.00")},
            ],
            current_target_weight=Decimal("10.00"),
            precision=Decimal("0.01"),
        )
        self.addCleanup(dialog.close)
        self.assertTrue(dialog.btn_apply.isEnabled())

        dialog.table.item(1, 1).setText("20.00")
        self.assertTrue(dialog._weights_modified)
        self.assertFalse(dialog.btn_apply.isEnabled())
        self.assertIn("weight must be greater than 0", dialog.summary_label.text())

        dialog.table.item(1, 1).setText("10.00")
        self.assertFalse(dialog._weights_modified)
        self.assertTrue(dialog.btn_apply.isEnabled())

        dialog.table.item(0, 2).setText("-5.00")
        self.assertFalse(dialog.btn_apply.isEnabled())
        self.assertIn("cannot be negative", dialog.summary_label.text())

    def test_dialog_comma_decimal_locale_preserves_precision_and_autofill(self) -> None:
        from PyQt6.QtCore import QLocale

        from tared.core.service import RecipeService

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_catalog_ingredient(CatalogIngredient("Citric Acid", Decimal("12.75")))
        service = RecipeService(repo)

        de_locale = QLocale(QLocale.Language.German, QLocale.Country.Germany)
        with patch("tared.ui.models.recipe_table_model.QLocale", return_value=de_locale):
            dialog = BatchByWeightDialog(
                repository=repo,
                service=service,
                current_ingredients=[
                    {"name": "Trace Active", "percentage": Decimal("0.001"), "cost_per_unit": Decimal("500.00")},
                    {"name": "Main Base", "percentage": Decimal("99.999"), "cost_per_unit": Decimal("2.50")},
                ],
                current_target_weight=Decimal("1.500"),
                precision=Decimal("0.001"),
            )
            self.addCleanup(dialog.close)

            self.assertEqual(dialog.table.item(0, 1).text(), "0,000")
            self.assertEqual(dialog.table.item(1, 1).text(), "1,500")
            self.assertEqual(dialog.table.item(1, 2).text(), "2,50")
            self.assertTrue(dialog.btn_apply.isEnabled())

            ingredients = dialog.get_ingredients()
            self.assertEqual(ingredients[1]["weight"], Decimal("1.500"))
            self.assertEqual(ingredients[1]["cost_per_unit"], Decimal("2.50"))

            dialog._add_row(name="", weight=Decimal("0.500"), cost=Decimal("0.00"))
            self.assertEqual(dialog.table.item(2, 2).text(), "0,00")
            dialog.table.item(2, 0).setText("Citric Acid")
            self.assertEqual(dialog.table.item(2, 2).text(), "12,75")

    def test_reopen_incomplete_draft_quantizes_non_multiple_fallback_weight_and_preserves_pct(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        dialog = BatchByWeightDialog(
            repository=repo,
            service=None,
            current_ingredients=[
                {"name": "Water", "percentage": Decimal("33.33"), "cost_per_unit": Decimal("0.10")},
                {"name": "Sugar", "percentage": Decimal("33.33"), "cost_per_unit": Decimal("1.00")},
            ],
            current_target_weight=Decimal("10.00"),
            precision=Decimal("0.01"),
        )
        self.addCleanup(dialog.close)

        self.assertEqual(dialog.table.item(0, 1).text(), "3.33")
        self.assertEqual(dialog.table.item(1, 1).text(), "3.33")
        self.assertTrue(dialog.btn_apply.isEnabled())

        dialog.table.item(0, 2).setText("0.20")
        self.assertFalse(dialog._weights_modified)
        self.assertEqual(dialog.get_ingredients()[0]["percentage"], Decimal("33.33"))

        dialog.auto_target_cb.setChecked(True)
        self.assertEqual(dialog.get_target_weight(), Decimal("6.66"))
        self.assertTrue(dialog.btn_apply.isEnabled())

    def test_dialog_rejects_target_weight_above_max_limit(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        dialog = BatchByWeightDialog(
            repository=repo,
            service=None,
            current_ingredients=[],
            current_target_weight=Decimal("1000.00"),
            precision=Decimal("0.01"),
        )
        self.addCleanup(dialog.close)
        dialog.table.setRowCount(0)
        dialog._add_row("Water", Decimal("1000000001.00"))

        self.assertFalse(dialog.btn_apply.isEnabled())
        self.assertIn("exceeds maximum limit", dialog.summary_label.text())

    def test_reopen_incomplete_draft_with_unquantized_model_weights_succeeds(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        draft = Recipe(
            id="incomplete-unquantized-weight",
            name="Incomplete Draft",
            is_draft=True,
            ingredients=(
                Ingredient("Water", Decimal("33.33"), Decimal("0.10")),
                Ingredient("Sugar", Decimal("33.33"), Decimal("1.00")),
            ),
        )
        self.model.set_precision(Decimal("0.01"))
        self.model.set_target_weight(Decimal("10.00"))
        self.model.load_recipe(draft)

        dialog = BatchByWeightDialog(
            repository=repo,
            service=None,
            current_ingredients=self.model.ingredients_data,
            current_target_weight=self.model.target_weight,
            precision=self.model.precision,
        )
        self.addCleanup(dialog.close)

        self.assertEqual(dialog.table.item(0, 1).text(), "3.33")
        self.assertEqual(dialog.table.item(1, 1).text(), "3.33")
        self.assertNotIn("not a multiple", dialog.summary_label.text())
        self.assertTrue(dialog.btn_apply.isEnabled())
        self.assertEqual(dialog.get_ingredients()[0]["percentage"], Decimal("33.33"))

    def test_dialog_allows_cleared_empty_unit_cost_as_zero_on_apply(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        dialog = BatchByWeightDialog(
            repository=repo,
            service=None,
            current_ingredients=[],
            current_target_weight=Decimal("1000.00"),
            precision=Decimal("0.01"),
        )
        self.addCleanup(dialog.close)
        dialog.table.setRowCount(0)
        dialog._add_row("Water", Decimal("100.00"), Decimal("5.00"))

        dialog.table.item(0, 2).setText("   ")
        self.assertTrue(dialog.btn_apply.isEnabled())

        with patch("PyQt6.QtWidgets.QMessageBox.warning") as mock_warn:
            with patch.object(dialog, "accept") as mock_accept:
                dialog._apply()
                mock_warn.assert_not_called()
                mock_accept.assert_called_once()

        ingredients = dialog.get_ingredients()
        self.assertEqual(ingredients[0]["cost_per_unit"], Decimal("0.00"))

    def test_main_window_batch_by_weight_status_bar_respects_precision_decimals(self) -> None:
        from unittest.mock import MagicMock

        from PyQt6.QtWidgets import QDialog

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        window = MainWindow(repo)
        self.addCleanup(window.close)
        window.precision_combo.setCurrentIndex(0)  # 0.001 g

        mock_dlg = MagicMock()
        mock_dlg.exec.return_value = QDialog.DialogCode.Accepted
        mock_dlg.has_user_edits.return_value = True
        mock_dlg.get_ingredients.return_value = [
            {"name": "Active", "weight": Decimal("0.125"), "cost_per_unit": Decimal("10.00")},
            {"name": "Water", "weight": Decimal("100.000"), "cost_per_unit": Decimal("0.10")},
        ]
        mock_dlg.get_target_weight.return_value = Decimal("100.125")

        with patch("tared.ui.views.weight_entry_dialog.BatchByWeightDialog", return_value=mock_dlg):
            window._open_batch_by_weight_dialog()

        self.assertIn("100.125 g total.", window.statusBar().currentMessage())

    def test_unchecking_auto_target_on_incomplete_draft_restores_original_percentages(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        dialog = BatchByWeightDialog(
            repository=repo,
            service=None,
            current_ingredients=[
                {"name": "Water", "percentage": Decimal("25.00"), "cost_per_unit": Decimal("0.10")},
                {"name": "Sugar", "percentage": Decimal("25.00"), "cost_per_unit": Decimal("1.00")},
            ],
            current_target_weight=Decimal("1000.00"),
            precision=Decimal("0.01"),
        )
        self.addCleanup(dialog.close)
        self.assertFalse(dialog.auto_target_cb.isChecked())

        dialog.auto_target_cb.setChecked(True)
        self.assertIn("Formula Total: 100.00%", dialog.summary_label.text())
        self.assertEqual(dialog.get_target_weight(), Decimal("500.00"))

        dialog.auto_target_cb.setChecked(False)
        self.assertIn("Formula Total: 50.00%", dialog.summary_label.text())
        self.assertEqual(dialog.get_target_weight(), Decimal("1000.00"))
        applied = dialog.get_ingredients()
        self.assertEqual(applied[0]["percentage"], Decimal("25.00"))
        self.assertEqual(applied[1]["percentage"], Decimal("25.00"))

    def test_incomplete_draft_target_mass_change_updates_model_weights(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        dialog = BatchByWeightDialog(
            repository=repo,
            service=None,
            current_ingredients=[
                {"name": "Water", "percentage": Decimal("25.00"), "cost_per_unit": Decimal("0.10")},
                {"name": "Sugar", "percentage": Decimal("25.00"), "cost_per_unit": Decimal("1.00")},
            ],
            current_target_weight=Decimal("1000.00"),
            precision=Decimal("0.01"),
        )
        self.addCleanup(dialog.close)
        self.assertFalse(dialog.auto_target_cb.isChecked())

        dialog.target_weight_spin.setValue(2000.00)
        self.assertTrue(dialog.has_user_edits())
        self.assertFalse(dialog._weights_modified)
        self.assertEqual(dialog.table.item(0, 1).text(), "500.00")
        self.assertEqual(dialog.table.item(1, 1).text(), "500.00")
        self.assertEqual(dialog.get_target_weight(), Decimal("2000.00"))

        self.model.set_ingredients_from_weights(dialog.get_ingredients(), target_weight=dialog.get_target_weight())
        self.assertIsNone(self.model.current_batch)
        self.assertEqual(self.model.ingredients_data[0]["weight"], Decimal("500.00"))
        self.assertEqual(self.model.ingredients_data[1]["weight"], Decimal("500.00"))
        self.assertEqual(self.model.data(self.model.index(0, 3)), "500.00")

    def test_dialog_spinbox_falls_back_cleanly_under_en_in_locale(self) -> None:
        from PyQt6.QtCore import QLocale

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        dialog = BatchByWeightDialog(
            repository=repo,
            service=None,
            current_ingredients=[{"name": "Base", "weight": Decimal("500.00"), "cost_per_unit": Decimal("1.00")}],
            current_target_weight=Decimal("1000.00"),
            precision=Decimal("0.01"),
        )
        self.addCleanup(dialog.close)
        dialog.auto_target_cb.setChecked(False)

        in_locale = QLocale(QLocale.Language.English, QLocale.Country.India)
        dialog.target_weight_spin.setLocale(in_locale)
        dialog.target_weight_spin.setValue(100000.05)
        dialog._recalculate()

        self.assertEqual(dialog._resolved_target_weight, Decimal("100000.05"))
        self.assertTrue(dialog.btn_apply.isEnabled())
        with patch.object(dialog, "accept") as mock_accept:
            dialog._apply()
            mock_accept.assert_called_once()

    def test_cleared_weight_on_preserved_trace_applies_while_huge_unit_cost_is_rejected(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        dialog = BatchByWeightDialog(
            repository=repo,
            service=None,
            current_ingredients=[
                {"name": "Micro Trace Active", "percentage": Decimal("0.001"), "cost_per_unit": Decimal("500.00")},
                {"name": "Bulk Excipient", "percentage": Decimal("99.999"), "cost_per_unit": Decimal("1.00")},
            ],
            current_target_weight=Decimal("10.00"),
            precision=Decimal("0.01"),
        )
        self.addCleanup(dialog.close)

        # Clearing trace row weight remains consistent between _recalculate() and _apply()
        dialog.table.item(0, 1).setText("")
        self.assertTrue(dialog.btn_apply.isEnabled())
        with patch("PyQt6.QtWidgets.QMessageBox.warning") as mock_warn:
            with patch.object(dialog, "accept") as mock_accept:
                dialog._apply()
                mock_warn.assert_not_called()
                mock_accept.assert_called_once()

        # Huge scientific notation unit cost must be rejected cleanly
        dialog.table.item(0, 2).setText("1e10000000")
        self.assertFalse(dialog.btn_apply.isEnabled())
        self.assertIn("exceeds maximum limit", dialog.summary_label.text())
        with patch("PyQt6.QtWidgets.QMessageBox.warning") as mock_warn:
            with patch.object(dialog, "accept") as mock_accept:
                dialog._apply()
                mock_warn.assert_called_once()
                mock_accept.assert_not_called()


if __name__ == "__main__":
    unittest.main()
