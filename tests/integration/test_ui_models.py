"""Integration tests verifying reactive PyQt6 table adapters, sorting, search, import collision, and deletion safety."""

import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SRC_DIR = Path(__file__).resolve().parents[2] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication, QMessageBox

    from tared.ui.models.recipe_table_model import RecipeTableModel
    from tared.ui.views.archived_formulas_dialog import ArchivedFormulasDialog
    from tared.ui.views.batch_history_dialog import BatchHistoryDialog
    from tared.ui.views.catalog_dialog import CatalogManagerDialog
    from tared.ui.views.catalog_picker_dialog import CatalogPickerDialog
    from tared.ui.views.main_window import MainWindow
    from tared.ui.views.print_dialog import PrintBatchSheetDialog

    qt_available = True
except ImportError:
    qt_available = False

from tared.config import ConfigManager
from tared.core.models import CatalogIngredient, Ingredient, Recipe
from tared.storage.base import DuplicateBatchNumberError
from tared.storage.json_repo import save_recipe_to_file
from tared.storage.sqlite_repo import SQLiteRecipeRepository
from tests.conftest import create_test_recipe


class TestRecipeTableModelIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not qt_available:
            return
        if QApplication.instance() is None:
            cls.app = QApplication(["test-runner"])
        else:
            cls.app = QApplication.instance()

    def setUp(self) -> None:
        if not qt_available:
            self.skipTest("PyQt6 not available; skipping Qt model integration tests.")

        self.question_patcher = patch(
            "PyQt6.QtWidgets.QMessageBox.question", return_value=QMessageBox.StandardButton.Discard
        )
        self.mock_question = self.question_patcher.start()
        self.addCleanup(self.question_patcher.stop)

        self.info_patcher = patch("PyQt6.QtWidgets.QMessageBox.information", return_value=QMessageBox.StandardButton.Ok)
        self.mock_info = self.info_patcher.start()
        self.addCleanup(self.info_patcher.stop)

        self.warn_patcher = patch("PyQt6.QtWidgets.QMessageBox.warning", return_value=QMessageBox.StandardButton.Ok)
        self.mock_warn = self.warn_patcher.start()
        self.addCleanup(self.warn_patcher.stop)

        self.crit_patcher = patch("PyQt6.QtWidgets.QMessageBox.critical", return_value=QMessageBox.StandardButton.Ok)
        self.mock_crit = self.crit_patcher.start()
        self.addCleanup(self.crit_patcher.stop)

        self.recovery_patcher = patch.object(PrintBatchSheetDialog, "_prompt_staging_recovery", return_value="close")
        self.mock_recovery = self.recovery_patcher.start()
        self.addCleanup(self.recovery_patcher.stop)

        self.model = RecipeTableModel()
        self.recipe = create_test_recipe()
        self.model.load_recipe(self.recipe)

    def test_open_catalog_and_sync_on_empty_draft_does_not_raise(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        # Clear to clean empty draft
        window._create_new_recipe()
        self.assertEqual(window.table_model.rowCount(), 0)
        self.assertIsNone(window._get_active_recipe_model())

        # Verify CatalogManagerDialog cleanly accepts None for current_recipe without raising
        dialog = CatalogManagerDialog(repo, window.service, current_recipe=None, parent=window)
        self.assertFalse(dialog.btn_learn.isEnabled())
        dialog.close()

        # Syncing costs on empty formula must not raise
        window._sync_costs_with_catalog()

    def test_new_draft_initializes_empty_table_without_dummy_components(self) -> None:
        self.model.new_draft("Clean Formula")
        self.assertEqual(self.model.rowCount(), 0)
        self.assertIsNone(self.model.current_batch)

    def test_precision_switcher_updates_spinbox_and_model(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        window.precision_combo.setCurrentIndex(0)
        self.assertEqual(window.table_model.precision, Decimal("0.001"))
        self.assertEqual(window.target_weight_spin.decimals(), 3)

        window.precision_combo.setCurrentIndex(3)
        self.assertEqual(window.table_model.precision, Decimal("1"))
        self.assertEqual(window.target_weight_spin.decimals(), 0)
        window.close()

    def test_manual_row_reordering_move_up_and_down(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        initial_row0 = window.table_model.ingredients_data[0]["name"]
        initial_row1 = window.table_model.ingredients_data[1]["name"]

        window.table_view.setCurrentIndex(window.table_model.index(1, 0))
        window._move_selected_row_up()

        self.assertEqual(window.table_model.ingredients_data[0]["name"], initial_row1)
        self.assertEqual(window.table_model.ingredients_data[1]["name"], initial_row0)
        self.assertTrue(window._is_dirty)

        window._move_selected_row_down()
        self.assertEqual(window.table_model.ingredients_data[0]["name"], initial_row0)
        self.assertEqual(window.table_model.ingredients_data[1]["name"], initial_row1)
        window.close()

    def test_micro_cost_per_kg_formatting(self) -> None:
        recipe = Recipe(
            name="Micro Trace Ingredient",
            ingredients=(
                Ingredient(name="Active Vitamin", percentage=Decimal("100.00"), cost_per_unit=Decimal("0.05")),
            ),
        )
        self.model.load_recipe(recipe)
        display_cost = self.model.data(self.model.index(0, 2))
        self.assertEqual(display_cost, "$0.05")

        scaled_line_cost = self.model.data(self.model.index(0, 5))
        self.assertEqual(scaled_line_cost, "$0.05")

    def test_save_then_navigate_synchronizes_sidebar_and_editor(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        r1 = Recipe(id="r1", name="Alpha Formula", ingredients=(Ingredient(name="A", percentage=Decimal("100.00")),))
        r2 = Recipe(id="r2", name="Beta Formula", ingredients=(Ingredient(name="B", percentage=Decimal("100.00")),))
        repo.save_recipe(r1)
        repo.save_recipe(r2)

        window = MainWindow(repo)
        self.addCleanup(window.close)
        self.assertEqual(window.name_input.text(), "Alpha Formula")

        window.name_input.setText("Alpha Modified")
        self.assertTrue(window._is_dirty)

        self.mock_question.return_value = QMessageBox.StandardButton.Save
        window.recipe_list.setCurrentRow(1)

        self.assertEqual(window.name_input.text(), "Beta Formula")
        current_item = window.recipe_list.currentItem()
        self.assertIsNotNone(current_item)
        assert current_item is not None
        self.assertEqual(current_item.data(Qt.ItemDataRole.UserRole), "r2")
        self.assertFalse(window._is_dirty)
        window.close()

    def test_discard_and_navigate_clears_dirty_state(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        r1 = Recipe(id="r1", name="Alpha Formula", ingredients=(Ingredient(name="A", percentage=Decimal("100.00")),))
        r2 = Recipe(id="r2", name="Beta Formula", ingredients=(Ingredient(name="B", percentage=Decimal("100.00")),))
        repo.save_recipe(r1)
        repo.save_recipe(r2)

        window = MainWindow(repo)
        self.addCleanup(window.close)
        window.name_input.setText("Alpha Modified")
        self.assertTrue(window._is_dirty)

        self.mock_question.return_value = QMessageBox.StandardButton.Discard
        window.recipe_list.setCurrentRow(1)

        self.assertEqual(window.name_input.text(), "Beta Formula")
        self.assertFalse(window._is_dirty)
        self.assertNotIn("[Unsaved Changes *]", window.windowTitle())
        window.close()

    def test_print_dialog_blocked_when_dirty(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        window.name_input.setText("Unsaved Title Change")
        self.assertTrue(window._is_dirty)

        self.mock_question.return_value = QMessageBox.StandardButton.Cancel
        with patch("tared.ui.views.main_window.PrintBatchSheetDialog") as mock_dlg:
            window._open_print_dialog()
            mock_dlg.assert_not_called()

        self.mock_question.return_value = QMessageBox.StandardButton.Discard
        window.close()
        repo.close()

    def test_print_dialog_prompts_to_save_unsaved_draft(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        self.mock_question.return_value = QMessageBox.StandardButton.Cancel
        with patch("tared.ui.views.main_window.PrintBatchSheetDialog") as mock_dlg:
            window._open_print_dialog()
            mock_dlg.assert_not_called()

        self.mock_question.return_value = QMessageBox.StandardButton.Discard
        window.close()
        repo.close()

    def test_canonical_manual_sequence_preserved_on_load(self) -> None:
        canonical_recipe = Recipe(
            name="Sequential Mixing Order",
            ingredients=(
                Ingredient(name="Phase 1 (10%)", percentage=Decimal("10.00")),
                Ingredient(name="Phase 2 (80%)", percentage=Decimal("80.00")),
                Ingredient(name="Phase 3 (10%)", percentage=Decimal("10.00")),
            ),
        )
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(canonical_recipe)

        window = MainWindow(repo)
        self.addCleanup(window.close)
        self.assertEqual(window.table_model.ingredients_data[0]["name"], "Phase 1 (10%)")
        self.assertEqual(window.table_model.ingredients_data[1]["name"], "Phase 2 (80%)")
        self.assertEqual(window.table_model.ingredients_data[2]["name"], "Phase 3 (10%)")

        assert window.table_model.current_batch is not None
        self.assertEqual(window.table_model.current_batch.items[0].cumulative_tare, Decimal("100.00"))
        self.assertEqual(window.table_model.current_batch.items[1].cumulative_tare, Decimal("900.00"))
        self.assertEqual(window.table_model.current_batch.items[2].cumulative_tare, Decimal("1000.00"))
        window.close()

    def test_delete_confirmed_removes_saved_recipe(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        self.mock_question.return_value = QMessageBox.StandardButton.Yes
        window._delete_current_recipe()

        self.assertIsNone(repo.get_recipe(self.recipe.id))
        self.assertFalse(window._is_dirty)
        window.close()

    def test_delete_draft_does_not_delete_saved_archive_record(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        self.mock_question.return_value = QMessageBox.StandardButton.Yes
        window._create_new_recipe()

        self.assertEqual(window.recipe_list.currentRow(), -1)
        window._delete_current_recipe()

        self.assertIsNotNone(repo.get_recipe(self.recipe.id))
        window.close()

    def test_sorting_marks_recipe_dirty_when_order_changes(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        self.assertFalse(window._is_dirty)
        window.sort_combo.setCurrentIndex(2)
        self.assertTrue(window._is_dirty)
        window.close()

    def test_save_bumps_revision_only_when_dirty(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        self.assertEqual(window.table_model.recipe_version, 1)

        window._save_current_recipe()
        saved = repo.get_recipe(self.recipe.id)
        assert saved is not None
        self.assertEqual(saved.version, 1)

        window.name_input.setText("Updated Title")
        window._save_current_recipe()

        saved2 = repo.get_recipe(self.recipe.id)
        assert saved2 is not None
        self.assertEqual(saved2.version, 2)
        self.assertIn("v2", window.rev_label.text())
        window.close()

    def test_zero_percentage_save_guard(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        window.table_model.setData(window.table_model.index(0, 1), "0.00")
        success = window._save_current_recipe()
        self.assertFalse(success)
        self.mock_warn.assert_called()
        window.close()

    def test_live_title_dirty_sync(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        self.assertNotIn("[Unsaved Changes *]", window.windowTitle())
        window.name_input.setText("Name Edited")
        self.assertIn("[Unsaved Changes *]", window.windowTitle())

        window._save_current_recipe()
        self.assertNotIn("[Unsaved Changes *]", window.windowTitle())
        window.close()

    def test_print_dialog_records_batch_audit_trail_atomically(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        batch = window.table_model.current_batch
        self.assertIsNotNone(batch)
        assert batch is not None

        dialog = PrintBatchSheetDialog(batch, parent=window, repository=repo)
        dialog.operator_input.setText("QC Chemist Mark")
        dialog.batch_num_input.setText("BAT-INTEG-001")

        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_target = str(Path(tmpdir) / "output.pdf")
            with patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName", return_value=(pdf_target, "PDF Files (*.pdf)")):
                dialog._export_pdf()
                self.assertTrue(Path(pdf_target).exists())
                self.assertGreater(Path(pdf_target).stat().st_size, 1000)

        batches = repo.list_batches(self.recipe.id)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["batch_number"], "BAT-INTEG-001")
        self.assertEqual(batches[0]["operator"], "QC Chemist Mark")
        window.close()

    def test_reimport_archived_recipe_collision_prompt(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        repo.delete_recipe(self.recipe.id)
        self.assertIsNone(repo.get_recipe(self.recipe.id))

        window = MainWindow(repo)
        self.addCleanup(window.close)

        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "reimport.json"
            incoming_recipe = Recipe(
                id=self.recipe.id,
                name="Unarchived Revision",
                version=self.recipe.version,
                ingredients=(Ingredient(name="Revived Component", percentage=Decimal("100.00")),),
            )
            save_recipe_to_file(incoming_recipe, file_path)

            with patch.object(window, "_prompt_import_collision", return_value="overwrite") as mock_collision:
                with patch(
                    "PyQt6.QtWidgets.QFileDialog.getOpenFileName", return_value=(str(file_path), "JSON Files (*.json)")
                ):
                    window._import_recipe()
                    mock_collision.assert_called_once()

            restored = repo.get_recipe(self.recipe.id)
            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.name, "Unarchived Revision")
            self.assertEqual(restored.version, 2)
            window.close()

    def test_sidebar_search_filter(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        r1 = Recipe(id="u1", name="Citrus Glaze", ingredients=(Ingredient(name="A", percentage=Decimal("100.00")),))
        r2 = Recipe(id="u2", name="Chili Oil", ingredients=(Ingredient(name="B", percentage=Decimal("100.00")),))
        repo.save_recipe(r1)
        repo.save_recipe(r2)

        window = MainWindow(repo)
        self.addCleanup(window.close)
        self.assertEqual(window.recipe_list.count(), 2)

        window.search_input.setText("chili")
        item0 = window.recipe_list.item(0)
        item1 = window.recipe_list.item(1)
        self.assertFalse(item0.isHidden())
        self.assertTrue(item1.isHidden())

        window.search_input.setText("")
        self.assertFalse(item0.isHidden())
        self.assertFalse(item1.isHidden())
        window.close()

    def test_pricing_update_bumps_recipe_version(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        self.assertEqual(window.table_model.recipe_version, 1)

        window.table_model.setData(window.table_model.index(0, 2), "12.50")
        self.assertTrue(window._is_dirty)
        window._save_current_recipe()

        saved = repo.get_recipe(self.recipe.id)
        assert saved is not None
        self.assertEqual(saved.version, 2)
        self.assertEqual(saved.ingredients[0].cost_per_unit, Decimal("12.50"))
        self.assertIn("v2", window.rev_label.text())

        window.table_model.setData(window.table_model.index(0, 1), "28.00")
        window.table_model.setData(window.table_model.index(1, 1), "30.00")
        self.assertTrue(window._is_dirty)
        window._save_current_recipe()

        saved2 = repo.get_recipe(self.recipe.id)
        assert saved2 is not None
        self.assertEqual(saved2.version, 3)
        self.assertIn("v3", window.rev_label.text())
        window.close()

    @patch("tared.ui.models.recipe_table_model.QLocale")
    def test_set_data_localized_comma_decimal_support(self, mock_qlocale) -> None:
        mock_instance = mock_qlocale.return_value
        mock_instance.groupSeparator.return_value = "."
        mock_instance.decimalPoint.return_value = ","

        success_pct = self.model.setData(self.model.index(0, 1), "33,33")
        self.assertTrue(success_pct)
        self.assertEqual(self.model.ingredients_data[0]["percentage"], Decimal("33.33"))

        success_cost = self.model.setData(self.model.index(0, 2), ",25")
        self.assertTrue(success_cost)
        self.assertEqual(self.model.ingredients_data[0]["cost_per_unit"], Decimal("0.25"))

        success2 = self.model.setData(self.model.index(0, 2), "1.250,50")
        self.assertTrue(success2)
        self.assertEqual(self.model.ingredients_data[0]["cost_per_unit"], Decimal("1250.50"))

        # In German locale, '1.5' is invalid grouping (not 3 digits), reject rather than misreading as 15
        self.assertFalse(self.model.setData(self.model.index(0, 1), "1.5"))

    @patch("tared.ui.models.recipe_table_model.QLocale")
    def test_set_data_en_us_locale_rejects_ambiguous_commas(self, mock_qlocale) -> None:
        mock_instance = mock_qlocale.return_value
        mock_instance.groupSeparator.return_value = ","
        mock_instance.decimalPoint.return_value = "."

        # 3,50 has 2 digits after comma; not a valid 3-digit group nor a decimal point in en_US
        self.assertFalse(self.model.setData(self.model.index(0, 2), "3,50"))
        self.assertFalse(self.model.setData(self.model.index(0, 1), "1,5"))

        # Valid 3-digit grouping is accepted
        self.assertTrue(self.model.setData(self.model.index(0, 2), "1,250.50"))
        self.assertEqual(self.model.ingredients_data[0]["cost_per_unit"], Decimal("1250.50"))

    def test_summary_label_surfaces_max_allocation_deviation(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)
        window.target_weight_spin.setValue(333.33)
        self.assertIn("Max Shift: at most", window.summary_label.text())
        window.close()

    def test_scientific_notation_and_micro_cost_formatting(self) -> None:
        success = self.model.setData(self.model.index(0, 2), "1e-4")
        self.assertTrue(success)
        self.assertEqual(self.model.ingredients_data[0]["cost_per_unit"], Decimal("0.0001"))
        self.assertEqual(self.model.data(self.model.index(0, 2)), "$0.0001")

        recipe = Recipe(
            name="Micro Precision Formula",
            ingredients=(
                Ingredient(name="Active Enzyme", percentage=Decimal("100.00"), cost_per_unit=Decimal("0.000001")),
            ),
        )
        self.model.load_recipe(recipe)
        self.assertEqual(self.model.data(self.model.index(0, 2)), "$0.000001")

    def test_print_dialog_live_duplicate_batch_number_disables_export(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)
        batch = window.table_model.current_batch
        assert batch is not None

        repo.record_batch(
            recipe_id=batch.recipe_id,
            recipe_version=batch.recipe_version,
            batch_number="BAT-RECORDED-99",
            operator="Op A",
            precision=batch.precision,
            target_weight=batch.target_weight,
            total_weight=batch.total_weight,
            total_cost=batch.total_cost,
        )

        dialog = PrintBatchSheetDialog(batch, parent=window, repository=repo)
        dialog.operator_input.setText("Op B")
        dialog.batch_num_input.setText("BAT-UNIQUE-01")
        self.assertTrue(dialog.pdf_btn.isEnabled())

        dialog.batch_num_input.setText("BAT-RECORDED-99")
        self.assertFalse(dialog.pdf_btn.isEnabled())
        self.assertIn("already recorded", dialog.pdf_btn.toolTip())

        dialog.close()
        window.close()

    def test_grid_aligns_cost_precision_with_batch(self) -> None:
        recipe = Recipe(
            name="Fractional Cost Formula",
            ingredients=(
                Ingredient(name="A", percentage=Decimal("50.00"), cost_per_unit=Decimal("3.00")),
                Ingredient(name="B", percentage=Decimal("50.00"), cost_per_unit=Decimal("0.25")),
            ),
        )
        self.model.load_recipe(recipe)
        self.assertEqual(self.model.cost_precision, 4)
        self.assertEqual(self.model.data(self.model.index(0, 5)), "$1.5000")
        self.assertEqual(self.model.data(self.model.index(1, 5)), "$0.1250")

    def test_print_dialog_post_commit_replace_failure_preserves_staging_file(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)
        batch = window.table_model.current_batch
        assert batch is not None

        dialog = PrintBatchSheetDialog(batch, parent=window, repository=repo)
        dialog.operator_input.setText("Operator Lead")
        dialog.batch_num_input.setText("BAT-REPLACE-FAIL-01")

        with tempfile.TemporaryDirectory() as tmpdir:
            dest_pdf = Path(tmpdir) / "final_sheet.pdf"
            with patch(
                "PyQt6.QtWidgets.QFileDialog.getSaveFileName", return_value=(str(dest_pdf), "PDF Files (*.pdf)")
            ):
                with patch("os.replace", side_effect=OSError("Simulated filesystem lock")):
                    dialog._export_pdf()

            self.assertTrue(repo.batch_exists("BAT-REPLACE-FAIL-01"))
            self.assertFalse(dest_pdf.exists())

            staging_files = list(Path(tmpdir).glob(".tmp_*"))
            self.assertEqual(len(staging_files), 1)
            self.assertGreater(staging_files[0].stat().st_size, 1000)

            self.mock_info.assert_called()
            last_call_args = self.mock_info.call_args[0]
            self.assertIn("preserved", last_call_args[2].lower())
            self.assertIn(str(staging_files[0]), last_call_args[2])

            staging_files[0].unlink()

        window.close()

    def test_print_dialog_post_commit_replace_retry_succeeds(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)
        batch = window.table_model.current_batch
        assert batch is not None

        dialog = PrintBatchSheetDialog(batch, parent=window, repository=repo)
        dialog.operator_input.setText("Operator Lead")
        dialog.batch_num_input.setText("BAT-RETRY-01")

        with tempfile.TemporaryDirectory() as tmpdir:
            dest_pdf = Path(tmpdir) / "final_retry.pdf"
            real_replace = os.replace
            attempts = 0

            def flaky_replace(src: str, dst: str) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise OSError("Temporary file lock")
                real_replace(src, dst)

            self.mock_recovery.side_effect = ["retry"]
            with patch(
                "PyQt6.QtWidgets.QFileDialog.getSaveFileName", return_value=(str(dest_pdf), "PDF Files (*.pdf)")
            ):
                with patch("os.replace", side_effect=flaky_replace):
                    dialog._export_pdf()

            self.assertTrue(repo.batch_exists("BAT-RETRY-01"))
            self.assertTrue(dest_pdf.exists())
            self.assertEqual(attempts, 2)

        window.close()

    def test_print_dialog_post_commit_replace_failure_recovers_to_alternate_location(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)
        batch = window.table_model.current_batch
        assert batch is not None

        dialog = PrintBatchSheetDialog(batch, parent=window, repository=repo)
        dialog.operator_input.setText("Operator Lead")
        dialog.batch_num_input.setText("BAT-RECOVER-01")

        with tempfile.TemporaryDirectory() as tmpdir:
            dest_pdf = Path(tmpdir) / "locked_sheet.pdf"
            alt_pdf = Path(tmpdir) / "alternate_copy.pdf"

            self.mock_recovery.return_value = "save_as"
            with patch(
                "PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                side_effect=[(str(dest_pdf), "PDF Files (*.pdf)"), (str(alt_pdf), "PDF Files (*.pdf)")],
            ):
                with patch("os.replace", side_effect=OSError("Simulated lock")):
                    dialog._export_pdf()

            self.assertTrue(repo.batch_exists("BAT-RECOVER-01"))
            self.assertTrue(alt_pdf.exists())
            self.assertGreater(alt_pdf.stat().st_size, 1000)

            staging_files = list(Path(tmpdir).glob(".tmp_*"))
            self.assertEqual(len(staging_files), 0)

        window.close()

    def test_print_dialog_post_commit_replace_failure_close_branch(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)
        batch = window.table_model.current_batch
        assert batch is not None

        dialog = PrintBatchSheetDialog(batch, parent=window, repository=repo)
        dialog.operator_input.setText("Operator Lead")
        dialog.batch_num_input.setText("BAT-CLOSE-01")

        with tempfile.TemporaryDirectory() as tmpdir:
            dest_pdf = Path(tmpdir) / "locked_close.pdf"

            self.mock_recovery.return_value = "close"
            with patch(
                "PyQt6.QtWidgets.QFileDialog.getSaveFileName", return_value=(str(dest_pdf), "PDF Files (*.pdf)")
            ):
                with patch("os.replace", side_effect=OSError("Permanent lock")):
                    dialog._export_pdf()

            self.assertTrue(repo.batch_exists("BAT-CLOSE-01"))
            self.assertFalse(dest_pdf.exists())

            staging_files = list(Path(tmpdir).glob(".tmp_*"))
            self.assertEqual(len(staging_files), 1)
            staging_files[0].unlink()

        window.close()

    def test_print_dialog_post_commit_save_copy_failure_preserves_staging(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)
        batch = window.table_model.current_batch
        assert batch is not None

        dialog = PrintBatchSheetDialog(batch, parent=window, repository=repo)
        dialog.operator_input.setText("Operator Lead")
        dialog.batch_num_input.setText("BAT-COPY-FAIL-01")

        with tempfile.TemporaryDirectory() as tmpdir:
            dest_pdf = Path(tmpdir) / "locked_sheet.pdf"
            alt_pdf = Path(tmpdir) / "alternate_copy.pdf"

            self.mock_recovery.return_value = "save_as"
            with patch(
                "PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                side_effect=[(str(dest_pdf), "PDF Files (*.pdf)"), (str(alt_pdf), "PDF Files (*.pdf)")],
            ):
                with patch("os.replace", side_effect=OSError("Simulated lock")):
                    with patch("shutil.copyfile", side_effect=OSError("Disk write fault")):
                        dialog._export_pdf()

            self.assertTrue(repo.batch_exists("BAT-COPY-FAIL-01"))
            self.assertFalse(alt_pdf.exists())

            staging_files = list(Path(tmpdir).glob(".tmp_*"))
            self.assertEqual(len(staging_files), 1)
            staging_files[0].unlink()

        window.close()

    def test_print_dialog_duplicate_error_disables_button_immediately(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)
        batch = window.table_model.current_batch
        assert batch is not None

        dialog = PrintBatchSheetDialog(batch, parent=window, repository=repo)
        dialog.operator_input.setText("Operator Lead")
        dialog.batch_num_input.setText("BAT-COLLIDE-RACE")

        with patch.object(repo, "record_batch", side_effect=DuplicateBatchNumberError("Simulated race")):
            with patch(
                "PyQt6.QtWidgets.QFileDialog.getSaveFileName", return_value=("/tmp/dummy.pdf", "PDF Files (*.pdf)")
            ):
                dialog._export_pdf()

        self.assertFalse(dialog.pdf_btn.isEnabled())
        self.assertTrue(dialog._batch_cache["BAT-COLLIDE-RACE"])
        dialog.close()
        window.close()

    def test_catalog_picker_dialog_inserts_multiple_items_to_model(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_catalog_ingredient(CatalogIngredient("Vanilla Extract", Decimal("45.00")))
        repo.save_catalog_ingredient(CatalogIngredient("Cocoa Powder", Decimal("12.50")))

        window = MainWindow(repo)
        self.addCleanup(window.close)
        initial_rows = window.table_model.rowCount()

        dialog = CatalogPickerDialog(repo, parent=window)
        self.assertEqual(dialog.table.rowCount(), 2)

        dialog.table.selectAll()
        self.assertTrue(dialog.btn_insert.isEnabled())

        dialog.accept()

        if dialog.selected_items:
            for item in dialog.selected_items:
                window.table_model.add_ingredient(name=item.name, cost_per_unit=item.cost_per_unit)

        self.assertEqual(window.table_model.rowCount(), initial_rows + 2)
        last_row = window.table_model.rowCount() - 1
        self.assertEqual(window.table_model.ingredients_data[last_row]["cost_per_unit"], Decimal("45.00"))
        self.assertEqual(window.table_model.ingredients_data[last_row - 1]["cost_per_unit"], Decimal("12.50"))
        window.close()

    def test_ingredient_delegate_provides_completer_from_catalog(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_catalog_ingredient(CatalogIngredient("Vanilla Extract", Decimal("45.00")))
        window = MainWindow(repo)
        self.addCleanup(window.close)

        delegate = window.table_view.itemDelegateForColumn(0)
        self.assertIsNotNone(delegate)
        self.assertIn("Vanilla Extract", delegate.get_names_cb())
        window.close()

    def test_add_selected_ingredient_to_catalog_action(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        window.table_view.setCurrentIndex(window.table_model.index(0, 0))
        self.assertIsNone(repo.get_catalog_ingredient("Material A"))

        window._add_selected_to_catalog()

        catalog_item = repo.get_catalog_ingredient("Material A")
        self.assertIsNotNone(catalog_item)
        assert catalog_item is not None
        self.assertEqual(catalog_item.cost_per_unit, Decimal("5.00"))

        window.table_model.setData(window.table_model.index(0, 2), "6.25")
        self.mock_question.return_value = QMessageBox.StandardButton.Yes
        window._add_selected_to_catalog()

        updated_item = repo.get_catalog_ingredient("Material A")
        assert updated_item is not None
        self.assertEqual(updated_item.cost_per_unit, Decimal("6.25"))
        window.close()

    def test_table_model_auto_prefills_cost_from_catalog_lookup(self) -> None:
        self.model.set_catalog_lookup(lambda name: Decimal("3.75") if name.lower() == "corn syrup" else None)

        self.model.add_ingredient("Component", Decimal("10.00"), Decimal("0.00"))
        row = self.model.rowCount() - 1
        self.assertEqual(self.model.ingredients_data[row]["cost_per_unit"], Decimal("0.00"))

        self.model.setData(self.model.index(row, 0), "Corn Syrup")
        self.assertEqual(self.model.ingredients_data[row]["cost_per_unit"], Decimal("3.75"))

    def test_table_model_auto_fill_emits_datachanged_covering_unit_cost_column(self) -> None:
        self.model.set_catalog_lookup(lambda name: Decimal("45.00") if name.lower() == "vanilla extract" else None)
        self.model.add_ingredient(name="", percentage=Decimal("10.00"), cost_per_unit=Decimal("0.00"))
        row = self.model.rowCount() - 1

        emitted_signals: list[tuple[int, int]] = []
        self.model.dataChanged.connect(
            lambda top_left, bottom_right, roles: emitted_signals.append((top_left.column(), bottom_right.column()))
        )

        self.model.setData(self.model.index(row, 0), "Vanilla Extract")

        self.assertEqual(self.model.ingredients_data[row]["cost_per_unit"], Decimal("45.00"))
        self.assertTrue(any(left == 0 and right >= 2 for left, right in emitted_signals))

    def test_catalog_manager_dialog_add_edit_and_recipe_learning(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        self.mock_question.return_value = QMessageBox.StandardButton.Yes
        dialog = CatalogManagerDialog(repo, window.service, current_recipe=self.recipe, parent=window)
        dialog._learn_from_recipe()
        self.assertEqual(dialog.table.rowCount(), 8)

        item = repo.get_catalog_ingredient("Material A")
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.cost_per_unit, Decimal("5.00"))

        dialog.close()
        window.close()

    def test_main_window_sync_costs_with_catalog_updates_recipe_draft(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        repo.save_catalog_ingredient(CatalogIngredient("Material A", Decimal("7.50")))

        self.mock_question.return_value = QMessageBox.StandardButton.Yes
        window._sync_costs_with_catalog()

        self.assertTrue(window._is_dirty)
        self.assertEqual(window.table_model.ingredients_data[0]["cost_per_unit"], Decimal("7.50"))
        window.close()

    def test_batch_history_dialog_lists_and_reprints_without_db_duplicate(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        repo.record_batch(
            recipe_id=self.recipe.id,
            recipe_version=1,
            batch_number="BAT-HIST-001",
            operator="Operator Lead",
            target_weight=Decimal("1000.00"),
            total_weight=Decimal("1000.00"),
            total_cost=Decimal("15.20"),
            items=[
                {
                    "ingredient_name": "Material A",
                    "scaled_weight": "500.00",
                    "cumulative_tare": "500.00",
                    "cost": "7.60",
                }
            ],
            notes="Initial calibration run.",
        )

        dialog = BatchHistoryDialog(self.recipe.id, self.recipe.name, repo, parent=window)
        self.assertEqual(dialog.table.rowCount(), 1)
        self.assertEqual(dialog.table.item(0, 0).text(), "BAT-HIST-001")
        self.assertEqual(dialog.table.item(0, 3).text(), "Operator Lead")

        with tempfile.TemporaryDirectory() as tmpdir:
            reprint_target = str(Path(tmpdir) / "reprint_sheet.pdf")
            with patch(
                "PyQt6.QtWidgets.QFileDialog.getSaveFileName", return_value=(reprint_target, "PDF Files (*.pdf)")
            ):
                dialog._reprint_selected_batch()

            self.assertTrue(Path(reprint_target).exists())
            self.assertGreater(Path(reprint_target).stat().st_size, 1000)

        self.assertEqual(len(repo.list_batches(self.recipe.id)), 1)
        window.close()

    def test_batch_history_reprint_uses_historical_revision_sop_and_name(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        r_v1 = Recipe(
            id="rec-hist-fidelity",
            name="Cold Process Emulsion",
            description="Blend at 10C with slow agitation.",
            version=1,
            ingredients=(Ingredient("Phase A", Decimal("100.00"), Decimal("2.00")),),
        )
        repo.save_recipe(r_v1)

        repo.record_batch(
            recipe_id=r_v1.id,
            recipe_version=1,
            batch_number="BAT-HIST-V1",
            operator="Operator Jane",
            target_weight=Decimal("1000.00"),
            total_weight=Decimal("1000.00"),
            total_cost=Decimal("2.00"),
            notes="Standard run.",
            items=[
                {
                    "ingredient_name": "Phase A",
                    "percentage": "100.00",
                    "scaled_weight": "1000.00",
                    "cumulative_tare": "1000.00",
                    "cost": "2.00",
                }
            ],
        )

        # Recipe subsequently updated to v2 with different name and compounding SOP
        r_v2 = Recipe(
            id=r_v1.id,
            name="Hot Process Gel",
            description="Boil at 105C with high shear.",
            version=2,
            ingredients=(Ingredient("Phase A", Decimal("100.00"), Decimal("3.00")),),
        )
        repo.save_recipe(r_v2)

        dialog = BatchHistoryDialog(r_v1.id, r_v2.name, repo)

        captured_batch = None

        def mock_export(batch, output_path, **kwargs):
            nonlocal captured_batch
            captured_batch = batch
            Path(output_path).write_bytes(b"%PDF-reprint")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_pdf = str(Path(tmpdir) / "reprint_v1.pdf")
            with patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName", return_value=(out_pdf, "PDF Files (*.pdf)")):
                with patch("tared.ui.views.batch_history_dialog.export_batch_sheet_to_pdf", side_effect=mock_export):
                    dialog._reprint_selected_batch()

        self.assertIsNotNone(captured_batch)
        assert captured_batch is not None
        self.assertEqual(captured_batch.recipe_name, "Cold Process Emulsion")
        self.assertEqual(captured_batch.recipe_description, "Blend at 10C with slow agitation.")
        self.assertEqual(captured_batch.recipe_version, 1)
        dialog.close()
        repo.close()

    def test_batch_history_reprint_reconstructs_discrete_allocation_deviation(self) -> None:
        from tared.core.calculator import compute_batch

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)

        calc_batch = compute_batch(self.recipe, target_weight=Decimal("333.33"), precision=Decimal("0.01"))
        self.assertGreater(calc_batch.max_allocation_deviation, Decimal("0"))

        repo.record_batch(
            recipe_id=self.recipe.id,
            recipe_version=1,
            batch_number="BAT-SHIFT-01",
            operator="Operator Lead",
            target_weight=calc_batch.target_weight,
            total_weight=calc_batch.total_weight,
            total_cost=calc_batch.total_cost,
            precision=calc_batch.precision,
            items=calc_batch.items,
        )

        dialog = BatchHistoryDialog(self.recipe.id, self.recipe.name, repo)
        captured_batch = None

        def mock_export(batch, output_path, **kwargs):
            nonlocal captured_batch
            captured_batch = batch
            Path(output_path).write_bytes(b"%PDF-reprint")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_pdf = str(Path(tmpdir) / "reprint_shift.pdf")
            with patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName", return_value=(out_pdf, "PDF Files (*.pdf)")):
                with patch("tared.ui.views.batch_history_dialog.export_batch_sheet_to_pdf", side_effect=mock_export):
                    dialog._reprint_selected_batch()

        self.assertIsNotNone(captured_batch)
        assert captured_batch is not None
        self.assertEqual(captured_batch.max_allocation_deviation, calc_batch.max_allocation_deviation)
        dialog.close()
        repo.close()

    def test_table_model_valid_parent_returns_zero_dimensions(self) -> None:
        idx = self.model.index(0, 0)
        self.assertTrue(idx.isValid())
        self.assertEqual(self.model.rowCount(idx), 0)
        self.assertEqual(self.model.columnCount(idx), 0)
        self.assertIsNone(self.model.data(self.model.index(0, 0, idx)))

    def test_shift_key_press_and_range_selection_does_not_crash(self) -> None:
        from PyQt6.QtCore import QEvent, QItemSelection, QItemSelectionModel
        from PyQt6.QtGui import QKeyEvent

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        key_event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Shift, Qt.KeyboardModifier.ShiftModifier)
        window.table_view.keyPressEvent(key_event)

        sel_model = window.table_view.selectionModel()
        idx_start = window.table_model.index(0, 0)
        idx_end = window.table_model.index(2, window.table_model.columnCount() - 1)
        selection = QItemSelection(idx_start, idx_end)
        sel_model.select(selection, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        self.assertEqual(len(sel_model.selectedRows()), 3)

    def test_batch_history_displays_revisions_and_restores_to_editor(self) -> None:
        from tared.core.service import RecipeService

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        service = RecipeService(repo)

        r_v1 = Recipe(
            id="rec-rev-ui",
            name="Emulsion Base",
            description="Phase 1 SOP",
            ingredients=(
                Ingredient("Water", Decimal("80.00"), Decimal("0.10")),
                Ingredient("Oil", Decimal("20.00"), Decimal("5.00")),
            ),
        )
        service.save_recipe(r_v1)

        r_v2 = Recipe(
            id="rec-rev-ui",
            name="Emulsion Base",
            description="Phase 2 SOP",
            ingredients=(
                Ingredient("Water", Decimal("75.00"), Decimal("0.10")),
                Ingredient("Oil", Decimal("25.00"), Decimal("5.00")),
            ),
        )
        service.save_recipe(r_v2)

        window = MainWindow(repo)
        self.addCleanup(window.close)
        self.assertEqual(window.table_model.recipe_version, 2)

        dialog = BatchHistoryDialog("rec-rev-ui", "Emulsion Base", repo, parent=window)
        self.assertEqual(dialog.tabs.currentIndex(), 1)
        self.assertEqual(dialog.revisions_table.rowCount(), 2)
        self.assertEqual(dialog.revisions_table.item(0, 0).text(), "v2")
        self.assertEqual(dialog.revisions_table.item(1, 0).text(), "v1")

        dialog.revision_restored.connect(window._restore_revision_to_editor)
        dialog.revisions_table.selectRow(1)
        dialog._restore_selected_revision()

        self.assertTrue(window._is_dirty)
        self.assertEqual(window.desc_input.text(), "Phase 1 SOP")
        self.assertEqual(window.table_model.ingredients_data[0]["percentage"], Decimal("80.00"))

        window._save_current_recipe()
        saved = repo.get_recipe("rec-rev-ui")
        assert saved is not None
        self.assertEqual(saved.version, 3)
        self.assertEqual(len(repo.get_recipe_revisions("rec-rev-ui")), 3)

    def test_batch_history_duplicate_revision_creates_new_formula(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)

        window = MainWindow(repo)
        self.addCleanup(window.close)

        dialog = BatchHistoryDialog(self.recipe.id, self.recipe.name, repo, parent=window)
        dialog.formula_duplicated.connect(window._on_formula_duplicated_from_history)
        dialog.revisions_table.selectRow(0)
        dialog._duplicate_selected_revision()
        dialog.close()

        recipes = repo.list_recipes()
        self.assertEqual(len(recipes), 2)
        self.assertTrue(any("(v1 Copy)" in rec.name for rec in recipes))

    def test_archived_formulas_dialog_restores_and_preserves_dirty_editor(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        r_active = Recipe(id="rec-act", name="Active Formula", ingredients=(Ingredient("A", Decimal("100.00")),))
        r_arch = Recipe(id="rec-arch", name="Old Formula", ingredients=(Ingredient("B", Decimal("100.00")),))
        repo.save_recipe(r_active)
        repo.save_recipe(r_arch)
        repo.delete_recipe("rec-arch")

        window = MainWindow(repo)
        self.addCleanup(window.close)
        window.name_input.setText("Unsaved Active Edit")
        self.assertTrue(window._is_dirty)

        arch_dialog = ArchivedFormulasDialog(repo, parent=window)
        arch_dialog.recipe_restored.connect(window._on_archived_recipe_restored)
        self.assertEqual(arch_dialog.table.rowCount(), 1)
        self.assertEqual(arch_dialog.table.item(0, 0).text(), "Old Formula")

        self.mock_question.return_value = QMessageBox.StandardButton.Yes
        arch_dialog._restore_selected()
        arch_dialog.close()

        self.assertIsNotNone(repo.get_recipe("rec-arch"))
        self.assertEqual(window.recipe_list.count(), 2)
        self.assertEqual(window.name_input.text(), "Unsaved Active Edit")
        self.assertTrue(window._is_dirty)

    def test_main_window_draft_mode_edit_finalize_and_discard_workflow(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        # 1. Create new formula and Save as Draft (< 100%, no nagging prompt)
        window.name_input.setText("Experimental Sauce")
        window.table_model.add_ingredient("Tomato", Decimal("40.00"), Decimal("2.00"))
        self.assertTrue(window._save_current_recipe(as_draft=True))

        draft_id = window._current_recipe_id
        assert draft_id is not None
        self.assertTrue(window.table_model.is_draft)
        self.assertIn("Draft", window.recipe_list.item(0).text())
        self.assertTrue(window.btn_discard_draft.isVisibleTo(window))
        self.assertEqual(window.btn_discard_draft.text(), "Delete Draft...")
        self.assertEqual(len(repo.get_recipe_revisions(draft_id)), 0)

        # 2. Edit draft multiple times -> stays at v1 Draft
        window.table_model.add_ingredient("Water", Decimal("60.00"), Decimal("0.10"))
        self.assertTrue(window._save_current_recipe())
        saved = repo.get_recipe(draft_id)
        assert saved is not None
        self.assertEqual(saved.version, 1)
        self.assertTrue(saved.is_draft)
        self.assertEqual(len(repo.get_recipe_revisions(draft_id)), 0)

        # 3. Finalize v1 -> locks v1 snapshot
        self.assertTrue(window._save_current_recipe(as_draft=False))
        finalized = repo.get_recipe(draft_id)
        assert finalized is not None
        self.assertEqual(finalized.version, 1)
        self.assertFalse(finalized.is_draft)
        self.assertEqual(len(repo.get_recipe_revisions(draft_id)), 1)

        # 4. Branch v2 Draft from v1, then discard back to v1
        self.assertTrue(window._save_current_recipe(as_draft=True))
        self.assertEqual(window.table_model.recipe_version, 2)
        self.assertTrue(window.table_model.is_draft)
        self.assertTrue(window.btn_discard_draft.isVisibleTo(window))
        self.assertEqual(window.btn_discard_draft.text(), "Discard Draft")

        window.name_input.setText("Abandoned v2 Experiment")
        self.mock_question.return_value = QMessageBox.StandardButton.Yes
        window._discard_current_draft()

        reverted = repo.get_recipe(draft_id)
        assert reverted is not None
        self.assertEqual(reverted.version, 1)
        self.assertFalse(reverted.is_draft)
        self.assertEqual(window.name_input.text(), "Experimental Sauce")

    def test_print_dialog_prompts_to_finalize_draft_before_printing(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        draft_rec = Recipe(
            id="rec-print-draft",
            name="Printable Draft",
            version=1,
            is_draft=True,
            ingredients=(Ingredient("Base", Decimal("100.00"), Decimal("2.00")),),
        )
        repo.save_recipe(draft_rec)

        window = MainWindow(repo)
        self.addCleanup(window.close)
        self.assertTrue(window.table_model.is_draft)

        self.mock_question.return_value = QMessageBox.StandardButton.Cancel
        with patch("tared.ui.views.main_window.PrintBatchSheetDialog") as mock_dlg:
            window._open_print_dialog()
            mock_dlg.assert_not_called()
        self.assertTrue(repo.get_recipe("rec-print-draft").is_draft)

        self.mock_question.return_value = QMessageBox.StandardButton.Yes
        with patch("tared.ui.views.main_window.PrintBatchSheetDialog") as mock_dlg:
            window._open_print_dialog()
            mock_dlg.assert_called_once()
        finalized = repo.get_recipe("rec-print-draft")
        assert finalized is not None
        self.assertFalse(finalized.is_draft)
        self.assertEqual(len(repo.get_recipe_revisions("rec-print-draft")), 1)

    def test_delete_v1_draft_from_main_window_and_archive_dialog(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        active_draft = Recipe(
            id="rec-active-draft-del",
            name="Active Test Draft",
            version=1,
            is_draft=True,
            ingredients=(Ingredient("Water", Decimal("30.00")),),
        )
        archived_draft = Recipe(
            id="rec-arch-draft-del",
            name="Archived Test Draft",
            version=1,
            is_draft=True,
            ingredients=(Ingredient("Sugar", Decimal("25.00")),),
        )
        repo.save_recipe(active_draft)
        repo.save_recipe(archived_draft)
        repo.delete_recipe("rec-arch-draft-del")

        window = MainWindow(repo)
        self.addCleanup(window.close)

        # Duplicate preserves is_draft=True
        window._duplicate_selected_recipe(active_draft)
        dupes = [r for r in repo.list_recipes() if "(Copy)" in r.name]
        self.assertEqual(len(dupes), 1)
        self.assertTrue(dupes[0].is_draft)

        # Delete active v1 draft via Delete Draft action
        self.mock_question.return_value = QMessageBox.StandardButton.Yes
        window._delete_draft_by_id("rec-active-draft-del", "Active Test Draft")
        self.assertIsNone(repo.get_recipe("rec-active-draft-del", include_archived=True))

        # Delete archived v1 draft from ArchivedFormulasDialog
        arch_dialog = ArchivedFormulasDialog(repo, parent=window)
        self.assertEqual(arch_dialog.table.rowCount(), 1)
        self.assertTrue(arch_dialog.btn_delete_permanent.isEnabled())
        self.assertFalse(arch_dialog.btn_history.isEnabled())
        arch_dialog._delete_selected_permanently()
        self.assertEqual(arch_dialog.table.rowCount(), 0)
        self.assertIsNone(repo.get_recipe("rec-arch-draft-del", include_archived=True))
        arch_dialog.close()

    def test_archive_dialog_allows_zero_batch_delete_and_blocks_with_batches(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)

        rec_zero = Recipe(
            id="rec-zero-batch",
            name="Zero Batch Archived",
            version=1,
            ingredients=(Ingredient("Water", Decimal("100.00")),),
        )
        rec_with_batch = Recipe(
            id="rec-has-batch",
            name="Has Batch Archived",
            version=1,
            ingredients=(Ingredient("Sugar", Decimal("100.00")),),
        )
        repo.save_recipe(rec_zero)
        repo.save_recipe(rec_with_batch)
        repo.record_batch(
            recipe_id=rec_with_batch.id,
            recipe_version=1,
            batch_number="BAT-ARCH-LOCK",
            operator="Op",
            target_weight="100.00",
            total_weight="100.00",
            total_cost="1.00",
        )
        repo.delete_recipe(rec_zero.id)
        repo.delete_recipe(rec_with_batch.id)

        arch_dialog = ArchivedFormulasDialog(repo)
        self.assertEqual(arch_dialog.table.rowCount(), 2)

        # Find row for rec_with_batch -> delete button disabled
        for row in range(arch_dialog.table.rowCount()):
            row_id = arch_dialog.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            arch_dialog.table.selectRow(row)
            if row_id == rec_with_batch.id:
                self.assertFalse(arch_dialog.btn_delete_permanent.isEnabled())
            else:
                self.assertTrue(arch_dialog.btn_delete_permanent.isEnabled())
                self.mock_question.return_value = QMessageBox.StandardButton.Yes
                arch_dialog._delete_selected_permanently()

        self.assertIsNone(repo.get_recipe(rec_zero.id, include_archived=True))
        self.assertIsNotNone(repo.get_recipe(rec_with_batch.id, include_archived=True))
        self.assertEqual(arch_dialog.table.rowCount(), 1)
        arch_dialog.close()

    def test_save_formula_on_incomplete_recipe_persists_as_draft_without_revision(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        window.name_input.setText("Incomplete Formula")
        window.table_model.add_ingredient("Water", Decimal("60.00"), Decimal("0.10"))

        self.mock_question.return_value = QMessageBox.StandardButton.Yes
        saved_ok = window._save_current_recipe(as_draft=False)
        self.assertTrue(saved_ok)

        saved_id = window._current_recipe_id
        assert saved_id is not None
        persisted = repo.get_recipe(saved_id)
        assert persisted is not None
        self.assertTrue(persisted.is_draft)
        self.assertEqual(persisted.version, 1)
        self.assertEqual(len(repo.get_recipe_revisions(saved_id)), 0)
        self.assertTrue(window.table_model.is_draft)

    def test_catalog_manager_rename_updates_existing_row_without_duplication(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        orig = CatalogIngredient("Sugar", Decimal("1.25"), supplier="Refinery")
        repo.save_catalog_ingredient(orig)

        dialog = CatalogManagerDialog(repo, MainWindow(repo).service)
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.table.rowCount(), 1)

        dialog.table.item(0, 0).setText("Cane Sugar")
        items = repo.list_catalog_ingredients()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, orig.id)
        self.assertEqual(items[0].name, "Cane Sugar")

    def test_corrupted_recipe_selection_and_context_menu_do_not_crash(self) -> None:
        from PyQt6.QtCore import QPoint

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        conn = repo._get_connection()
        conn.execute(
            "INSERT INTO recipes (id, name, version, updated_at, is_archived) "
            "VALUES ('bad-ui-1', 'Corrupt Shell', 1, '', 0);"
        )

        window = MainWindow(repo)
        self.addCleanup(window.close)
        self.assertEqual(window.recipe_list.count(), 1)
        self.assertIn("Corrupted", window.rev_label.text())

        rect = window.recipe_list.visualItemRect(window.recipe_list.item(0))
        window._on_recipe_list_context_menu(rect.center() if rect.isValid() else QPoint(5, 5))
        window._open_batch_history_dialog()
        window._delete_current_recipe()

    def test_changing_target_weight_or_precision_does_not_mark_dirty(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        self.assertFalse(window._is_dirty)
        window.target_weight_spin.setValue(2500.00)
        self.assertFalse(window._is_dirty)
        window.precision_combo.setCurrentIndex(0)
        self.assertFalse(window._is_dirty)

    def test_noop_enter_on_name_and_scaled_weight_preserves_percentage_and_clean_state(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        exact_pct = Decimal("100") / Decimal("3")
        recipe = Recipe(
            id="rec-thirds-noop",
            name="Exact Thirds",
            ingredients=(
                Ingredient("A", exact_pct, Decimal("1.00")),
                Ingredient("B", exact_pct, Decimal("1.00")),
                Ingredient("C", Decimal("100") - exact_pct - exact_pct, Decimal("1.00")),
            ),
        )
        repo.save_recipe(recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        idx_name = window.table_model.index(0, 0)
        self.assertTrue(window.table_model.setData(idx_name, "A"))
        self.assertFalse(window._is_dirty)

        idx_weight = window.table_model.index(0, 3)
        current_weight_edit = window.table_model.data(idx_weight, Qt.ItemDataRole.EditRole)
        self.assertTrue(window.table_model.setData(idx_weight, current_weight_edit))
        self.assertFalse(window._is_dirty)
        self.assertEqual(window.table_model.ingredients_data[0]["percentage"], exact_pct)

    def test_open_catalog_and_sync_with_incomplete_rows_does_not_raise(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        repo.save_catalog_ingredient(CatalogIngredient("Material A", Decimal("7.50")))

        window = MainWindow(repo)
        self.addCleanup(window.close)

        window.table_model.add_ingredient(name="", percentage=Decimal("0.00"))
        window.table_model.add_ingredient(name="Pending Material", percentage=Decimal("0.00"))

        with patch("tared.ui.views.main_window.CatalogManagerDialog") as mock_dlg:
            window._open_catalog_dialog()
            mock_dlg.assert_called_once()
            passed_recipe = mock_dlg.call_args[1]["current_recipe"]
            self.assertIsNotNone(passed_recipe)
            self.assertEqual(len(passed_recipe.ingredients), len(self.recipe.ingredients))

        self.mock_question.return_value = QMessageBox.StandardButton.Yes
        window._sync_costs_with_catalog()
        self.assertEqual(window.table_model.ingredients_data[0]["cost_per_unit"], Decimal("7.50"))

    def test_data_transfer_signals_preserve_dirty_editor_and_reset_clears_dirty_state(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        window.name_input.setText("Unsaved Active Edit")
        self.assertTrue(window._is_dirty)

        # Catalog import and recipe import must not discard unsaved editor changes
        window._on_catalog_imported_from_transfer()
        self.assertEqual(window.name_input.text(), "Unsaved Active Edit")
        self.assertTrue(window._is_dirty)

        extra_recipe = Recipe(
            id="rec-extra",
            name="Imported Extra",
            ingredients=(Ingredient("X", Decimal("100.00")),),
        )
        repo.save_recipe(extra_recipe)
        window._on_recipes_imported_from_transfer()
        self.assertEqual(window.name_input.text(), "Unsaved Active Edit")
        self.assertTrue(window._is_dirty)
        self.assertEqual(window.recipe_list.count(), 2)

        # Workspace reset clears DB and resets dirty state cleanly without prompting
        repo.reset_workspace_data()
        window._on_workspace_restored()
        self.assertFalse(window._is_dirty)
        self.assertIsNone(window._current_recipe_id)
        self.assertEqual(window.recipe_list.count(), 0)

    def test_sync_costs_and_target_spinbox_succeed_under_comma_decimal_locale(self) -> None:
        from PyQt6.QtCore import QLocale

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        repo.save_catalog_ingredient(CatalogIngredient("Material A", Decimal("7.50")))

        window = MainWindow(repo)
        self.addCleanup(window.close)

        de_locale = QLocale(QLocale.Language.German, QLocale.Country.Germany)
        window.target_weight_spin.setLocale(de_locale)
        window.target_weight_spin.setValue(1500.25)
        self.assertEqual(window.table_model.target_weight, Decimal("1500.25"))

        with patch("tared.ui.models.recipe_table_model.QLocale", return_value=de_locale):
            self.assertEqual(window.table_model.data(window.table_model.index(0, 1), Qt.ItemDataRole.EditRole), "29,00")
            self.mock_question.return_value = QMessageBox.StandardButton.Yes
            window._sync_costs_with_catalog()
            self.assertEqual(window.table_model.ingredients_data[0]["cost_per_unit"], Decimal("7.50"))

    def test_archived_dialog_filter_avoids_selecting_hidden_row(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        r1 = Recipe(id="arch-a", name="Alpha Archived", ingredients=(Ingredient("A", Decimal("100.00")),))
        r2 = Recipe(id="arch-b", name="Beta Archived", ingredients=(Ingredient("B", Decimal("100.00")),))
        repo.save_recipe(r1)
        repo.save_recipe(r2)
        repo.delete_recipe("arch-a")
        repo.delete_recipe("arch-b")

        dlg = ArchivedFormulasDialog(repo)
        self.addCleanup(dlg.close)
        dlg.search_input.setText("Beta")

        selected = dlg.table.selectionModel().selectedRows()
        self.assertEqual(len(selected), 1)
        self.assertFalse(dlg.table.isRowHidden(selected[0].row()))
        self.assertEqual(dlg.table.item(selected[0].row(), 0).text(), "Beta Archived")

        dlg.search_input.setText("NoMatchXYZ")
        self.assertEqual(len(dlg.table.selectionModel().selectedRows()), 0)
        self.assertFalse(dlg.btn_restore.isEnabled())
        self.assertFalse(dlg.btn_delete_permanent.isEnabled())

    def test_catalog_manager_preserves_subcent_costs_on_metadata_edit(self) -> None:
        from tared.core.service import RecipeService

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_catalog_ingredient(CatalogIngredient("Trace Enzyme", Decimal("0.0035"), supplier="BioCorp"))

        dialog = CatalogManagerDialog(repo, RecipeService(repo))
        self.addCleanup(dialog.close)

        self.assertEqual(dialog.table.item(0, 1).text(), "0.0035")
        dialog.table.item(0, 2).setText("BioCorp Global")

        saved = repo.get_catalog_ingredient("Trace Enzyme")
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved.cost_per_unit, Decimal("0.0035"))
        self.assertEqual(saved.supplier, "BioCorp Global")

    def test_catalog_manager_add_item_avoids_name_collision_and_supports_comma_locale(self) -> None:
        from PyQt6.QtCore import QLocale

        from tared.core.service import RecipeService

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_catalog_ingredient(CatalogIngredient("New Material 2", Decimal("9.50")))

        dialog = CatalogManagerDialog(repo, RecipeService(repo))
        self.addCleanup(dialog.close)
        dialog._add_item()

        items = repo.list_catalog_ingredients()
        self.assertEqual(len(items), 2)
        existing_2 = repo.get_catalog_ingredient("New Material 2")
        assert existing_2 is not None
        self.assertEqual(existing_2.cost_per_unit, Decimal("9.50"))
        self.assertIsNotNone(repo.get_catalog_ingredient("New Material 3"))

        de_locale = QLocale(QLocale.Language.German, QLocale.Country.Germany)
        with patch("tared.ui.models.recipe_table_model.QLocale", return_value=de_locale):
            dialog.table.item(0, 1).setText("12,75")
        updated = repo.get_catalog_ingredient(dialog.table.item(0, 0).text())
        assert updated is not None
        self.assertEqual(updated.cost_per_unit, Decimal("12.75"))

    def test_corrupted_recipe_can_be_repaired_in_place_or_archived(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        conn = repo._get_connection()
        conn.execute(
            "INSERT INTO recipes (id, name, version, updated_at, is_archived) "
            "VALUES ('bad-fix-1', 'Broken Shell', 1, '', 0);"
        )
        conn.execute(
            "INSERT INTO recipes (id, name, version, updated_at, is_archived) "
            "VALUES ('bad-arch-1', 'Broken Archive', 1, '', 0);"
        )

        window = MainWindow(repo)
        self.addCleanup(window.close)
        self.assertEqual(window.recipe_list.count(), 2)

        # 1. Select 'bad-fix-1', repair it, and save -> updates existing ID in place
        window._load_selected_recipe("bad-fix-1")
        window.table_model.add_ingredient("Water", Decimal("100.00"), Decimal("0.10"))
        self.assertTrue(window._save_current_recipe())

        repaired = repo.get_recipe("bad-fix-1")
        self.assertIsNotNone(repaired)
        assert repaired is not None
        self.assertEqual(repaired.name, "Broken Shell")
        self.assertEqual(len(repo.list_recipes()), 1)

        # 2. Select 'bad-arch-1' and archive it directly
        window._load_selected_recipe("bad-arch-1")
        self.mock_question.return_value = QMessageBox.StandardButton.Yes
        window._delete_current_recipe()
        self.assertEqual(len(repo.get_corrupted_recipes()), 0)
        self.assertEqual(window.recipe_list.count(), 1)

    def test_restore_revision_to_active_draft_displays_accurate_label(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        recipe = Recipe(
            id="rec-draft-rev-lbl",
            name="Draft Label Recipe",
            version=2,
            is_draft=True,
            ingredients=(Ingredient("Water", Decimal("100.00")),),
        )
        repo.save_recipe(recipe)

        window = MainWindow(repo)
        self.addCleanup(window.close)
        self.assertTrue(window.table_model.is_draft)

        past_rev = Recipe(
            id="rec-draft-rev-lbl",
            name="Draft Label Recipe",
            version=1,
            ingredients=(Ingredient("Water", Decimal("100.00")),),
        )
        window._restore_revision_to_editor(past_rev)
        self.assertIn("Save to update v2 Draft", window.rev_label.text())

    def test_batch_history_dialog_formats_cost_and_receives_config_from_archive(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        r = Recipe(id="rec-arch-cfg", name="Archived With Batch", ingredients=(Ingredient("A", Decimal("100.00")),))
        repo.save_recipe(r)
        repo.record_batch(
            recipe_id=r.id,
            recipe_version=1,
            batch_number="BAT-CFG-01",
            operator="Op",
            target_weight=Decimal("100.00"),
            total_weight=Decimal("100.00"),
            total_cost=Decimal("12.5"),
        )
        repo.delete_recipe(r.id)

        custom_cfg = ConfigManager()
        arch_dialog = ArchivedFormulasDialog(repo, config=custom_cfg)
        self.addCleanup(arch_dialog.close)
        self.assertEqual(arch_dialog.config, custom_cfg)

        batch_dialog = BatchHistoryDialog(r.id, r.name, repo, config=custom_cfg)
        self.addCleanup(batch_dialog.close)
        self.assertEqual(batch_dialog.config, custom_cfg)
        self.assertEqual(batch_dialog.table.item(0, 5).text(), "$12.50")

    def test_batch_history_reprint_legacy_items_without_raw_weight_or_pct_has_zero_shift(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)

        repo.record_batch(
            recipe_id=self.recipe.id,
            recipe_version=1,
            batch_number="BAT-LEGACY-NOSHIFT",
            operator="Operator Lead",
            target_weight=Decimal("1000.00"),
            total_weight=Decimal("1000.00"),
            total_cost=Decimal("15.20"),
            items=[
                {
                    "ingredient_name": "Material A",
                    "scaled_weight": "500.00",
                    "cumulative_tare": "500.00",
                    "cost": "7.60",
                },
                {
                    "ingredient_name": "Material B",
                    "scaled_weight": "500.00",
                    "cumulative_tare": "1000.00",
                    "cost": "7.60",
                },
            ],
        )

        dialog = BatchHistoryDialog(self.recipe.id, self.recipe.name, repo)
        self.addCleanup(dialog.close)
        captured_batch = None

        def mock_export(batch, output_path, **kwargs):
            nonlocal captured_batch
            captured_batch = batch
            Path(output_path).write_bytes(b"%PDF-legacy")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_pdf = str(Path(tmpdir) / "legacy_reprint.pdf")
            with patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName", return_value=(out_pdf, "PDF Files (*.pdf)")):
                with patch("tared.ui.views.batch_history_dialog.export_batch_sheet_to_pdf", side_effect=mock_export):
                    dialog._reprint_selected_batch()

        self.assertIsNotNone(captured_batch)
        assert captured_batch is not None
        self.assertEqual(captured_batch.max_allocation_deviation, Decimal("0"))

    def test_batch_history_duplicate_incomplete_revision_creates_draft(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)

        # Insert an incomplete historical revision (< 100%) bypassing service validation
        conn = repo._get_connection()
        conn.execute(
            "INSERT INTO recipe_revisions (id, recipe_id, version, name, description, ingredients_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?);",
            (
                "rev-incomplete-1",
                self.recipe.id,
                2,
                "Incomplete Legacy Rev",
                "Legacy draft snapshot",
                '[{"name": "Water", "percentage": "60.00", "cost_per_unit": "0.10"}]',
                "2026-01-01T00:00:00Z",
            ),
        )

        window = MainWindow(repo)
        self.addCleanup(window.close)

        dialog = BatchHistoryDialog(self.recipe.id, self.recipe.name, repo, parent=window)
        self.addCleanup(dialog.close)
        dialog.formula_duplicated.connect(window._on_formula_duplicated_from_history)
        dialog.revisions_table.selectRow(0)  # v2 incomplete revision
        dialog._duplicate_selected_revision()

        recipes = repo.list_recipes()
        self.assertEqual(len(recipes), 2)
        dupes = [r for r in recipes if "(v2 Copy)" in r.name]
        self.assertEqual(len(dupes), 1)
        self.assertTrue(dupes[0].is_draft)

    def test_catalog_dialogs_clear_or_deselect_hidden_rows_on_filter(self) -> None:
        from tared.core.service import RecipeService

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_catalog_ingredient(CatalogIngredient("Alpha Gum", Decimal("10.00")))
        repo.save_catalog_ingredient(CatalogIngredient("Beta Sugar", Decimal("2.00")))

        mgr = CatalogManagerDialog(repo, RecipeService(repo))
        self.addCleanup(mgr.close)
        mgr.table.selectRow(0)  # Alpha Gum
        mgr.search_input.setText("Beta")
        self.assertTrue(mgr.table.isRowHidden(0))
        self.assertEqual(len(mgr.table.selectionModel().selectedRows()), 0)
        self.mock_question.return_value = QMessageBox.StandardButton.Yes
        mgr._remove_selected()
        self.assertIsNotNone(repo.get_catalog_ingredient("Alpha Gum"))

        picker = CatalogPickerDialog(repo)
        self.addCleanup(picker.close)
        picker.table.selectAll()
        self.assertTrue(picker.btn_insert.isEnabled())
        picker.search_input.setText("Beta")
        self.assertTrue(picker.table.isRowHidden(0))
        self.assertFalse(picker.table.selectionModel().isRowSelected(0, picker.table.rootIndex()))
        self.assertTrue(picker.btn_insert.isEnabled())
        picker.accept()
        self.assertEqual([item.name for item in picker.selected_items], ["Beta Sugar"])

        picker2 = CatalogPickerDialog(repo)
        self.addCleanup(picker2.close)
        picker2.table.selectRow(0)
        picker2.search_input.setText("NoMatchXYZ")
        self.assertFalse(picker2.btn_insert.isEnabled())

    def test_print_dialog_disables_export_button_after_successful_commit(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)
        batch = window.table_model.current_batch
        assert batch is not None

        dialog = PrintBatchSheetDialog(batch, parent=window, repository=repo)
        self.addCleanup(dialog.close)
        dialog.operator_input.setText("QC Chemist")
        dialog.batch_num_input.setText("BAT-DISABLE-AFTER-OK")
        self.assertTrue(dialog.pdf_btn.isEnabled())

        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_target = str(Path(tmpdir) / "committed.pdf")
            with patch(
                "PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                return_value=(pdf_target, "PDF Files (*.pdf)"),
            ):
                dialog._export_pdf()

        self.assertFalse(dialog.pdf_btn.isEnabled())
        self.assertIn("already recorded", dialog.pdf_btn.toolTip())

    def test_catalog_picker_subcent_cost_and_add_item_clears_hiding_filter(self) -> None:
        from tared.core.service import RecipeService

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_catalog_ingredient(CatalogIngredient("Trace Enzyme", Decimal("0.0035")))

        picker = CatalogPickerDialog(repo)
        self.addCleanup(picker.close)
        self.assertEqual(picker.table.item(0, 1).text(), "$0.0035/kg")

        mgr = CatalogManagerDialog(repo, RecipeService(repo))
        self.addCleanup(mgr.close)
        mgr.search_input.setText("Trace")
        mgr._add_item()
        self.assertEqual(mgr.search_input.text(), "")
        selected = mgr.table.selectionModel().selectedRows()
        self.assertEqual(len(selected), 1)
        self.assertFalse(mgr.table.isRowHidden(selected[0].row()))

    def test_settings_dialog_rejects_regular_file_as_backup_dir(self) -> None:
        from PyQt6.QtCore import QSettings

        from tared.ui.views.settings_dialog import SettingsDialog

        with tempfile.TemporaryDirectory() as tmpdir:
            ini_path = Path(tmpdir) / "settings.ini"
            regular_file = Path(tmpdir) / "not_a_dir.txt"
            regular_file.write_text("placeholder", encoding="utf-8")

            cfg = ConfigManager(QSettings(str(ini_path), QSettings.Format.IniFormat))
            dlg = SettingsDialog(cfg)
            self.addCleanup(dlg.close)
            dlg.backup_enable_cb.setChecked(True)
            dlg.backup_dir_input.setText(str(regular_file))
            dlg._save_settings()

            self.mock_warn.assert_called()
            self.assertFalse(cfg.auto_backup_enabled)
            self.assertEqual(cfg.auto_backup_dir, "")

    def test_export_draft_formula_to_json_and_reimport_roundtrip(self) -> None:
        from tared.storage.json_repo import load_recipe_from_file

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        window.name_input.setText("Exportable Draft")
        window.table_model.add_ingredient("Water", Decimal("60.00"), Decimal("0.10"))
        self.assertTrue(window._save_current_recipe(as_draft=True))

        with tempfile.TemporaryDirectory() as tmpdir:
            out_json = Path(tmpdir) / "draft_export.json"
            with patch(
                "PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                return_value=(str(out_json), "JSON Files (*.json)"),
            ):
                window._export_recipe()

            self.assertTrue(out_json.is_file())
            loaded = load_recipe_from_file(out_json)
            self.assertTrue(loaded.is_draft)
            self.assertEqual(loaded.name, "Exportable Draft")

            reimported = window.service.import_recipe(loaded, collision_mode="copy")
            self.assertTrue(reimported.is_draft)

    def test_open_print_dialog_and_discard_draft_handle_corrupted_state_gracefully(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        conn = repo._get_connection()
        conn.execute(
            "INSERT INTO recipes (id, name, version, updated_at, is_archived, is_draft) "
            "VALUES ('bad-print-1', 'Corrupt Shell', 1, '', 0, 0);"
        )

        window = MainWindow(repo)
        self.addCleanup(window.close)
        window._load_selected_recipe("bad-print-1")

        self.mock_question.return_value = QMessageBox.StandardButton.Cancel
        with patch("tared.ui.views.main_window.PrintBatchSheetDialog") as mock_dlg:
            window._open_print_dialog()
            mock_dlg.assert_not_called()

        # Malformed revision JSON during discard_draft must show critical dialog instead of raising
        draft_rec = Recipe(
            id="bad-rev-draft",
            name="Bad Rev Draft",
            version=2,
            is_draft=True,
            ingredients=(Ingredient("Water", Decimal("100.00")),),
        )
        repo.save_recipe(draft_rec)
        conn.execute(
            "INSERT INTO recipe_revisions (id, recipe_id, version, name, description, ingredients_json, created_at) "
            "VALUES ('rev-bad-json', 'bad-rev-draft', 1, 'Bad', '', '{not-json', '2026-01-01T00:00:00Z');"
        )
        window._load_selected_recipe("bad-rev-draft")
        self.mock_question.return_value = QMessageBox.StandardButton.Yes
        window._discard_current_draft()
        self.mock_crit.assert_called()

    def test_table_model_set_target_weight_isolated_from_ambient_decimal_context(self) -> None:
        import decimal

        orig_ctx = decimal.getcontext().copy()
        try:
            decimal.getcontext().prec = 4
            decimal.getcontext().rounding = decimal.ROUND_DOWN
            self.model.set_target_weight(Decimal("1234.56"))
        finally:
            decimal.setcontext(orig_ctx)

        # 29.00% of 1234.56 = 358.0224 (7 significant digits, preserved beyond ambient prec=4)
        self.assertEqual(self.model.ingredients_data[0]["weight"], Decimal("358.0224"))

    def test_global_exception_hook_skips_messagebox_on_background_thread(self) -> None:
        import threading

        import tared.__main__ as tared_main

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "crash.log"
            orig_log = tared_main.LOG_FILE
            tared_main.LOG_FILE = log_file
            try:
                with patch(
                    "PyQt6.QtWidgets.QMessageBox",
                    side_effect=AssertionError("QMessageBox must not be created off the GUI thread"),
                ):
                    import io
                    err = RuntimeError("background thread failure")
                    sink = io.StringIO()
                    orig_stderr = sys.__stderr__
                    sys.__stderr__ = sink
                    try:
                        t = threading.Thread(
                            target=lambda: tared_main.global_exception_hook(RuntimeError, err, None)
                        )
                        t.start()
                        t.join(timeout=2.0)
                        self.assertFalse(t.is_alive())
                    finally:
                        sys.__stderr__ = orig_stderr

                self.assertTrue(log_file.is_file())
                self.assertIn("background thread failure", log_file.read_text(encoding="utf-8"))
            finally:
                tared_main.LOG_FILE = orig_log

    def test_sort_combo_resets_silently_on_weight_formulation_and_row_additions(self) -> None:
        from unittest.mock import MagicMock

        from PyQt6.QtWidgets import QDialog

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)

        window = MainWindow(repo)
        self.addCleanup(window.close)

        window.sort_combo.setCurrentIndex(2)
        self.assertEqual(window.table_model.ingredients_data[0]["name"], "Material H")

        mock_dlg = MagicMock()
        mock_dlg.exec.return_value = QDialog.DialogCode.Accepted
        mock_dlg.has_user_edits.return_value = True
        mock_dlg.get_ingredients.return_value = [
            {"name": "First", "weight": Decimal("200.00"), "cost_per_unit": Decimal("1.00")},
            {"name": "Second", "weight": Decimal("800.00"), "cost_per_unit": Decimal("1.00")},
        ]
        mock_dlg.get_target_weight.return_value = Decimal("1000.00")

        with patch("tared.ui.views.weight_entry_dialog.BatchByWeightDialog", return_value=mock_dlg):
            window._open_batch_by_weight_dialog()

        self.assertEqual(window.sort_combo.currentIndex(), 0)
        self.assertEqual([it["name"] for it in window.table_model.ingredients_data], ["First", "Second"])

        window.sort_combo.setCurrentIndex(1)
        self.assertEqual([it["name"] for it in window.table_model.ingredients_data], ["Second", "First"])

        window.btn_add_row.click()
        self.assertEqual(window.sort_combo.currentIndex(), 0)
        self.assertEqual([it["name"] for it in window.table_model.ingredients_data], ["Second", "First", ""])

    @patch("tared.ui.models.recipe_table_model.QLocale")
    def test_set_data_de_de_locale_grouped_integer_and_zero_weight_edit_role(self, mock_qlocale) -> None:
        mock_instance = mock_qlocale.return_value
        mock_instance.groupSeparator.return_value = "."
        mock_instance.decimalPoint.return_value = ","

        self.model.set_precision(Decimal("0.001"))
        self.model.setData(self.model.index(0, 2), Decimal("1.500"))
        self.assertEqual(self.model.data(self.model.index(0, 2), Qt.ItemDataRole.EditRole), "1,500")

        # Unchanged localized string remains a no-op
        self.assertTrue(self.model.setData(self.model.index(0, 2), "1,500"))
        self.assertEqual(self.model.ingredients_data[0]["cost_per_unit"], Decimal("1.500"))

        # Valid German thousands-grouped integer "1.500" must parse as 1500, not match str(current_cost)
        self.assertTrue(self.model.setData(self.model.index(0, 2), "1.500"))
        self.assertEqual(self.model.ingredients_data[0]["cost_per_unit"], Decimal("1500"))

        # Zero-weight EditRole respects precision_decimals
        self.model.add_ingredient("Zero Row", Decimal("0.00"))
        zero_row = self.model.rowCount() - 1
        self.assertEqual(self.model.data(self.model.index(zero_row, 3), Qt.ItemDataRole.EditRole), "0,000")

    def test_archived_dialog_refreshes_selection_state_after_deleting_top_row(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)

        r_zero = Recipe(
            id="arch-top-zero",
            name="Alpha Zero Batch",
            updated_at="2026-01-02T00:00:00Z",
            ingredients=(Ingredient("Water", Decimal("100.00")),),
        )
        r_batch = Recipe(
            id="arch-second-batch",
            name="Beta Has Batch",
            updated_at="2026-01-01T00:00:00Z",
            ingredients=(Ingredient("Sugar", Decimal("100.00")),),
        )
        repo.save_recipe(r_zero)
        repo.save_recipe(r_batch)
        repo.record_batch(
            recipe_id=r_batch.id,
            recipe_version=1,
            batch_number="BAT-TOP-ROW-GUARD",
            operator="Op",
            target_weight="100.00",
            total_weight="100.00",
            total_cost="1.00",
        )
        repo.delete_recipe(r_zero.id)
        repo.delete_recipe(r_batch.id)

        dlg = ArchivedFormulasDialog(repo)
        self.addCleanup(dlg.close)
        self.assertEqual(dlg.table.item(0, 0).text(), "Alpha Zero Batch")
        self.assertTrue(dlg.btn_delete_permanent.isEnabled())

        self.mock_question.return_value = QMessageBox.StandardButton.Yes
        dlg._delete_selected_permanently()

        self.assertEqual(dlg.table.rowCount(), 1)
        self.assertEqual(dlg.table.item(0, 0).text(), "Beta Has Batch")
        self.assertFalse(dlg.btn_delete_permanent.isEnabled())
        self.assertTrue(dlg.btn_history.isEnabled())

    def test_on_archived_recipe_restored_handles_corrupted_recipe_gracefully(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        conn = repo._get_connection()
        conn.execute(
            "INSERT INTO recipes (id, name, version, updated_at, is_archived) "
            "VALUES ('corrupt-arch-restore', 'Corrupt Archived', 1, '', 1);"
        )

        window = MainWindow(repo)
        self.addCleanup(window.close)
        self.assertTrue(repo.restore_recipe("corrupt-arch-restore"))
        window._on_archived_recipe_restored("corrupt-arch-restore")
        self.assertIn("corrupt-arch-restore", window.statusBar().currentMessage())

    def test_restore_revision_after_duplicate_syncs_sidebar_selection_and_draft_controls(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        r_v1 = Recipe(
            id="rec-dup-restore",
            name="Base Formula",
            description="v1 SOP",
            version=1,
            ingredients=(Ingredient("Water", Decimal("100.00"), Decimal("0.10")),),
        )
        repo.save_recipe(r_v1)

        window = MainWindow(repo)
        self.addCleanup(window.close)
        dialog = BatchHistoryDialog(r_v1.id, r_v1.name, repo, parent=window)
        self.addCleanup(dialog.close)
        dialog.formula_duplicated.connect(window._on_formula_duplicated_from_history)
        dialog.revision_restored.connect(window._restore_revision_to_editor)

        dialog.revisions_table.selectRow(0)
        dialog._duplicate_selected_revision()
        self.assertNotEqual(window._current_recipe_id, r_v1.id)

        dialog._restore_selected_revision()
        self.assertEqual(window._current_recipe_id, r_v1.id)
        current_item = window.recipe_list.currentItem()
        self.assertIsNotNone(current_item)
        assert current_item is not None
        self.assertEqual(current_item.data(Qt.ItemDataRole.UserRole), r_v1.id)
        self.assertTrue(window._is_dirty)

    def test_catalog_manager_cleared_cost_normalizes_to_zero(self) -> None:
        from tared.core.service import RecipeService

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_catalog_ingredient(CatalogIngredient("Sugar", Decimal("2.50")))

        dialog = CatalogManagerDialog(repo, RecipeService(repo))
        self.addCleanup(dialog.close)
        self.mock_warn.reset_mock()

        dialog.table.item(0, 1).setText("   ")
        self.mock_warn.assert_not_called()
        saved = repo.get_catalog_ingredient("Sugar")
        assert saved is not None
        self.assertEqual(saved.cost_per_unit, Decimal("0.00"))
        self.assertEqual(dialog.table.item(0, 1).text(), "0.00")

        # Name edit with leading/trailing whitespace trims cell text in place
        dialog.table.item(0, 0).setText("   Refined Sugar   ")
        self.assertEqual(dialog.table.item(0, 0).text(), "Refined Sugar")
        saved_trimmed = repo.get_catalog_ingredient("Refined Sugar")
        self.assertIsNotNone(saved_trimmed)

    def test_target_weight_spinbox_falls_back_under_non_three_digit_grouping_locale(self) -> None:
        from PyQt6.QtCore import QLocale

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        window = MainWindow(repo)
        self.addCleanup(window.close)

        in_locale = QLocale(QLocale.Language.English, QLocale.Country.India)
        window.target_weight_spin.setLocale(in_locale)
        window.target_weight_spin.setValue(100000.00)
        self.assertEqual(window.table_model.target_weight, Decimal("100000.00"))

    def test_huge_scientific_unit_cost_rejected_in_table_model_and_catalog_manager(self) -> None:
        from tared.core.service import RecipeService

        orig_cost = self.model.ingredients_data[0]["cost_per_unit"]
        rejected: list[str] = []
        self.model.edit_rejected.connect(rejected.append)
        self.assertFalse(self.model.setData(self.model.index(0, 2), "1e10000000"))
        self.assertEqual(self.model.ingredients_data[0]["cost_per_unit"], orig_cost)
        self.assertEqual(len(rejected), 1)
        self.assertIn("cannot exceed", rejected[0])

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_catalog_ingredient(CatalogIngredient("Sugar", Decimal("2.50")))
        dialog = CatalogManagerDialog(repo, RecipeService(repo))
        self.addCleanup(dialog.close)
        self.mock_warn.reset_mock()

        dialog.table.item(0, 1).setText("1e10000000")
        self.mock_warn.assert_called_once()
        saved = repo.get_catalog_ingredient("Sugar")
        assert saved is not None
        self.assertEqual(saved.cost_per_unit, Decimal("2.50"))

    def test_restore_revision_and_reprint_handle_corrupted_active_recipe_and_cancel_keeps_dialog_open(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        r_v1 = Recipe(
            id="rec-corrupt-hist",
            name="Stable Formula",
            description="v1 SOP",
            version=1,
            ingredients=(Ingredient("Water", Decimal("100.00"), Decimal("0.10")),),
        )
        repo.save_recipe(r_v1)
        repo.record_batch(
            recipe_id=r_v1.id,
            recipe_version=1,
            batch_number="BAT-CORRUPT-FALLBACK",
            operator="Op",
            target_weight="100.00",
            total_weight="100.00",
            total_cost="0.10",
        )

        window = MainWindow(repo)
        self.addCleanup(window.close)

        # 1. Declining unsaved-changes confirmation keeps BatchHistoryDialog open
        window.name_input.setText("Unsaved Dirty Title")
        self.assertTrue(window._is_dirty)
        self.mock_question.return_value = QMessageBox.StandardButton.No
        hist_dlg = BatchHistoryDialog(
            r_v1.id,
            r_v1.name,
            repo,
            parent=window,
            confirm_restore_cb=window._confirm_replace_unsaved_changes,
        )
        self.addCleanup(hist_dlg.close)
        hist_dlg.revision_restored.connect(lambda rev: window._restore_revision_to_editor(rev, confirm_if_dirty=False))
        hist_dlg.revisions_table.selectRow(0)
        with patch.object(hist_dlg, "accept") as mock_accept:
            hist_dlg._restore_selected_revision()
            mock_accept.assert_not_called()
        self.assertEqual(window.name_input.text(), "Unsaved Dirty Title")

        # 2. Corrupt active ingredients row while revision/batch history remains intact
        conn = repo._get_connection()
        conn.execute("DELETE FROM ingredients WHERE recipe_id = ?;", (r_v1.id,))
        window._restore_revision_to_editor(r_v1, confirm_if_dirty=False)
        self.assertEqual(window.name_input.text(), "Stable Formula")
        self.assertTrue(window._save_current_recipe())
        repaired = repo.get_recipe(r_v1.id)
        assert repaired is not None
        self.assertEqual(repaired.version, 2)

        # 3. Reprint fallback handles corrupted active recipe gracefully when snapshot lookup is bypassed
        with patch.object(repo, "get_recipe_revision", return_value=None):
            with patch.object(repo, "get_recipe_revisions", return_value=[]):
                conn.execute("DELETE FROM ingredients WHERE recipe_id = ?;", (r_v1.id,))
                with tempfile.TemporaryDirectory() as tmpdir:
                    out_pdf = str(Path(tmpdir) / "fallback_reprint.pdf")
                    with patch(
                        "PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(out_pdf, "PDF Files (*.pdf)"),
                    ):
                        hist_dlg._reprint_selected_batch()
                    self.assertTrue(Path(out_pdf).is_file())

    def test_batch_history_handles_none_fields_and_negative_cost_decimals_safely(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        repo.record_batch(
            recipe_id=self.recipe.id,
            recipe_version=1,
            batch_number="BAT-NULL-UI-01",
            operator="Op",
            target_weight=Decimal("100.00"),
            total_weight=Decimal("100.00"),
            total_cost=Decimal("2.50"),
        )
        with patch.object(
            repo,
            "list_batches",
            return_value=[
                {
                    "id": "b-null-ui",
                    "recipe_id": self.recipe.id,
                    "recipe_version": None,
                    "batch_number": "BAT-NULL-UI-01",
                    "operator": None,
                    "precision": "0.01",
                    "target_weight": "100.00",
                    "total_weight": "100.00",
                    "total_cost": "2.50",
                    "items_json": "[]",
                    "notes": None,
                    "created_at": None,
                    "cost_decimals": -1,
                }
            ],
        ):
            dlg = BatchHistoryDialog(self.recipe.id, self.recipe.name, repo)
            self.addCleanup(dlg.close)
            self.assertEqual(dlg.table.item(0, 2).text(), "v1")
            self.assertEqual(dlg.table.item(0, 3).text(), "")
            self.assertEqual(dlg.table.item(0, 6).text(), "")

            with tempfile.TemporaryDirectory() as tmpdir:
                out_pdf = str(Path(tmpdir) / "null_ui_reprint.pdf")
                with patch(
                    "PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                    return_value=(out_pdf, "PDF Files (*.pdf)"),
                ):
                    dlg._reprint_selected_batch()
                self.assertTrue(Path(out_pdf).is_file())

    def test_batch_history_handles_null_cost_in_revision_snapshot(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        conn = repo._get_connection()
        conn.execute(
            "INSERT INTO recipe_revisions (id, recipe_id, version, name, description, ingredients_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?);",
            (
                "rev-null-cost",
                self.recipe.id,
                2,
                "Null Cost Rev",
                "",
                '[{"name": "Water", "percentage": "100.00", "cost_per_unit": null}]',
                "2026-01-01T00:00:00Z",
            ),
        )
        dlg = BatchHistoryDialog(self.recipe.id, self.recipe.name, repo)
        self.addCleanup(dlg.close)
        dlg.revisions_table.selectRow(0)
        self.assertEqual(dlg.rev_ingredients_table.item(0, 2).text(), "$0.00")
        rev_recipe = dlg._build_recipe_from_revision(dlg._revisions[0])
        self.assertEqual(rev_recipe.ingredients[0].cost_per_unit, Decimal("0.00"))

    def test_reprint_null_created_at_timestamp_and_catalog_upsert_preserves_metadata(self) -> None:
        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(self.recipe)
        repo.save_catalog_ingredient(
            CatalogIngredient("Material A", Decimal("5.00"), supplier="Primary Supplier", sku="SKU-A")
        )
        repo.record_batch(
            recipe_id=self.recipe.id,
            recipe_version=1,
            batch_number="BAT-NULL-TS",
            operator="Op",
            target_weight=Decimal("100.00"),
            total_weight=Decimal("100.00"),
            total_cost=Decimal("1.00"),
        )

        window = MainWindow(repo)
        self.addCleanup(window.close)
        window.table_model.setData(window.table_model.index(0, 2), "9.99")
        window.table_view.setCurrentIndex(window.table_model.index(0, 0))
        self.mock_question.return_value = QMessageBox.StandardButton.Yes
        window._add_selected_to_catalog()

        cat_item = repo.get_catalog_ingredient("Material A")
        assert cat_item is not None
        self.assertEqual(cat_item.cost_per_unit, Decimal("9.99"))
        self.assertEqual(cat_item.supplier, "Primary Supplier")
        self.assertEqual(cat_item.sku, "SKU-A")

        dlg = BatchHistoryDialog(self.recipe.id, self.recipe.name, repo)
        self.addCleanup(dlg.close)
        dlg._batches[0]["created_at"] = None
        captured_ts = ""

        def mock_export(batch, output_path, timestamp_str="", **kwargs):
            nonlocal captured_ts
            captured_ts = timestamp_str
            Path(output_path).write_bytes(b"%PDF-mock")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_pdf = str(Path(tmpdir) / "null_ts_reprint.pdf")
            with patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName", return_value=(out_pdf, "PDF Files (*.pdf)")):
                with patch("tared.ui.views.batch_history_dialog.export_batch_sheet_to_pdf", side_effect=mock_export):
                    dlg._reprint_selected_batch()

        self.assertTrue(captured_ts.startswith("Reprinted: "))
        self.assertNotIn("None", captured_ts)


if __name__ == "__main__":
    unittest.main()
