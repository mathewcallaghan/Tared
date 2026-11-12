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
    from tared.ui.views.batch_history_dialog import BatchHistoryDialog
    from tared.ui.views.catalog_dialog import CatalogManagerDialog
    from tared.ui.views.catalog_picker_dialog import CatalogPickerDialog
    from tared.ui.views.main_window import MainWindow
    from tared.ui.views.print_dialog import PrintBatchSheetDialog

    qt_available = True
except ImportError:
    qt_available = False

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


if __name__ == "__main__":
    unittest.main()
