import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[2] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

"""End-to-end integration tests verifying automated cloud backup on exit,
disaster recovery, and data transfer workflows."""

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtCore import QSettings
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication, QMessageBox

from tared.config import ConfigManager
from tared.core.models import CatalogIngredient, Ingredient, Recipe
from tared.storage.sqlite_repo import SQLiteRecipeRepository
from tared.storage.workspace_io import restore_workspace_from_json
from tared.ui.views.data_transfer_dialog import DataTransferDialog
from tared.ui.views.main_window import MainWindow
from tests.conftest import create_test_recipe


class TestCloudBackupWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if QApplication.instance() is None:
            cls.app = QApplication(["test-workflow-runner"])
        else:
            cls.app = QApplication.instance()

    def setUp(self) -> None:
        # Prevent modal dialogs from blocking headless execution
        self.question_patcher = patch(
            "PyQt6.QtWidgets.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes
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

        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)

        # Isolated storage paths
        self.db_path = self.base_path / "active_recipes.db"
        self.ini_path = self.base_path / "settings.ini"
        self.cloud_sync_dir = self.base_path / "Nextcloud" / "Tared_Backups"
        self.cloud_sync_dir.mkdir(parents=True)

        # Isolated config
        self.qsettings = QSettings(str(self.ini_path), QSettings.Format.IniFormat)
        self.config = ConfigManager(self.qsettings)
        self.config.auto_backup_enabled = True
        self.config.auto_backup_dir = str(self.cloud_sync_dir)

        # Active repository & window
        self.repo = SQLiteRecipeRepository(self.db_path)
        self.recipe = create_test_recipe()
        self.repo.save_recipe(self.recipe)
        self.repo.save_catalog_ingredient(
            CatalogIngredient("Bulk Cocoa", Decimal("8.50"), supplier="Direct Cocoa Corp")
        )
        self.repo.record_batch(
            recipe_id=self.recipe.id,
            recipe_version=1,
            batch_number="BAT-E2E-001",
            operator="Operator Lead",
            target_weight=Decimal("1000.00"),
            total_weight=Decimal("1000.00"),
            total_cost=Decimal("12.50"),
            notes="Workflow calibration batch.",
        )

        self.window = MainWindow(self.repo, config=self.config)

    def tearDown(self) -> None:
        self.window.close()
        self.repo.close()
        self.temp_dir.cleanup()

    def test_auto_backup_triggers_immediately_on_recipe_save_and_batch_commit(self) -> None:
        backup_file = self.cloud_sync_dir / "tared_workspace.json"
        self.assertFalse(backup_file.exists())

        # 1. Mutate active recipe and save -> triggers immediate automated backup
        self.window.name_input.setText("Auto Backup Verified Formula")
        saved = self.window._save_current_recipe()
        self.assertTrue(saved)
        self.assertTrue(backup_file.is_file())

        backup_data = json.loads(backup_file.read_text(encoding="utf-8"))
        saved_names = [r["name"] for r in backup_data["recipes"]]
        self.assertIn("Auto Backup Verified Formula", saved_names)

        # 2. Commit a new production batch sheet -> triggers immediate automated backup
        batch = self.window.table_model.current_batch
        assert batch is not None
        from tared.ui.views.print_dialog import PrintBatchSheetDialog
        dialog = PrintBatchSheetDialog(batch, parent=self.window, repository=self.repo, config=self.config)
        dialog.operator_input.setText("Automated QC Operator")
        dialog.batch_num_input.setText("BAT-AUTO-SYNC-01")

        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_out = str(Path(tmpdir) / "sheet.pdf")
            with patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName", return_value=(pdf_out, "PDF Files (*.pdf)")):
                dialog._export_pdf()

        dialog.close()

        # Verify updated workspace backup file contains the new batch
        updated_backup_data = json.loads(backup_file.read_text(encoding="utf-8"))
        batch_numbers = [b["batch_number"] for b in updated_backup_data["batches"]]
        self.assertIn("BAT-AUTO-SYNC-01", batch_numbers)

    def test_full_cloud_backup_and_disaster_recovery_roundtrip(self) -> None:
        # 1. Close application: triggers automated exit backup to cloud sync folder
        close_evt = QCloseEvent()
        self.window.closeEvent(close_evt)
        self.assertTrue(close_evt.isAccepted())

        backup_file = self.cloud_sync_dir / "tared_workspace.json"
        self.assertTrue(backup_file.is_file())

        backup_data = json.loads(backup_file.read_text(encoding="utf-8"))
        self.assertEqual(backup_data["schema_version"], "1.0")
        self.assertEqual(len(backup_data["recipes"]), 1)
        self.assertEqual(len(backup_data["catalog"]), 1)
        self.assertEqual(len(backup_data["batches"]), 1)

        # 2. Simulate disaster / fresh machine install
        fresh_db_path = self.base_path / "fresh_machine.db"
        fresh_repo = SQLiteRecipeRepository(fresh_db_path)
        self.assertEqual(len(fresh_repo.list_recipes()), 0)
        self.assertEqual(len(fresh_repo.list_catalog_ingredients()), 0)

        # 3. Restore workspace from cloud sync file
        restore_workspace_from_json(fresh_repo, backup_file)

        # 4. Open fresh MainWindow on restored database
        fresh_window = MainWindow(fresh_repo, config=self.config)
        self.assertEqual(fresh_window.recipe_list.count(), 1)
        self.assertEqual(fresh_window.name_input.text(), self.recipe.name)

        catalog_items = fresh_repo.list_catalog_ingredients()
        self.assertEqual(len(catalog_items), 1)
        self.assertEqual(catalog_items[0].name, "Bulk Cocoa")

        batches = fresh_repo.list_batches(self.recipe.id)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["batch_number"], "BAT-E2E-001")

        fresh_window.close()
        fresh_repo.close()

    def test_selective_recipe_export_and_batch_import(self) -> None:
        r2 = Recipe(
            id="rec-secondary-02",
            name="Vanilla Coating",
            version=1,
            ingredients=(Ingredient("Vanilla Extract", Decimal("100.00"), Decimal("45.00")),),
        )
        self.repo.save_recipe(r2)

        transfer_dlg = DataTransferDialog(self.repo, self.window.service, config=self.config)
        export_folder = self.base_path / "exported_recipes"
        export_folder.mkdir()

        # Select all recipes and export into directory
        transfer_dlg._select_all_recipes()
        with patch("PyQt6.QtWidgets.QFileDialog.getExistingDirectory", return_value=str(export_folder)):
            transfer_dlg._export_selected_recipes()

        exported_files = list(export_folder.glob("*.json"))
        self.assertEqual(len(exported_files), 2)

        # Clear active recipes
        self.repo.delete_recipe(self.recipe.id)
        self.repo.delete_recipe(r2.id)
        self.assertEqual(len(self.repo.list_recipes()), 0)

        # Batch import both files with collision prompt resolved to 'overwrite' (No button)
        file_paths = [str(f) for f in exported_files]
        with patch("PyQt6.QtWidgets.QFileDialog.getOpenFileNames", return_value=(file_paths, "JSON Files (*.json)")):
            with patch("PyQt6.QtWidgets.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
                transfer_dlg._import_recipes()

        restored_recipes = self.repo.list_recipes()
        self.assertEqual(len(restored_recipes), 2)
        names = {r.name for r in restored_recipes}
        self.assertIn("Vanilla Coating", names)

        transfer_dlg.close()


if __name__ == "__main__":
    unittest.main()
