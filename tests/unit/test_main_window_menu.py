"""Unit and integration tests for MainWindow menu bar, context menus, duplication, and automated exit backup."""

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
from tared.storage.sqlite_repo import SQLiteRecipeRepository
from tared.ui.views.main_window import MainWindow
from tests.conftest import create_test_recipe


class TestMainWindowMenuAndBackup(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if QApplication.instance() is None:
            cls.app = QApplication(["test-menu-runner"])
        else:
            cls.app = QApplication.instance()

    def setUp(self) -> None:
        self.question_patcher = patch(
            "PyQt6.QtWidgets.QMessageBox.question", return_value=QMessageBox.StandardButton.Save
        )
        self.mock_question = self.question_patcher.start()
        self.addCleanup(self.question_patcher.stop)

        self.info_patcher = patch("PyQt6.QtWidgets.QMessageBox.information", return_value=QMessageBox.StandardButton.Ok)
        self.mock_info = self.info_patcher.start()
        self.addCleanup(self.info_patcher.stop)

        self.warn_patcher = patch("PyQt6.QtWidgets.QMessageBox.warning", return_value=QMessageBox.StandardButton.Yes)
        self.mock_warn = self.warn_patcher.start()
        self.addCleanup(self.warn_patcher.stop)

        self.crit_patcher = patch("PyQt6.QtWidgets.QMessageBox.critical", return_value=QMessageBox.StandardButton.Ok)
        self.mock_crit = self.crit_patcher.start()
        self.addCleanup(self.crit_patcher.stop)

        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_menu.db"
        self.ini_path = Path(self.temp_dir.name) / "test_settings.ini"
        self.qsettings = QSettings(str(self.ini_path), QSettings.Format.IniFormat)
        self.config = ConfigManager(self.qsettings)

        self.repo = SQLiteRecipeRepository(self.db_path)
        self.recipe = create_test_recipe()
        self.repo.save_recipe(self.recipe)

        self.window = MainWindow(self.repo, config=self.config)

    def tearDown(self) -> None:
        self.window.close()
        self.repo.close()
        self.temp_dir.cleanup()

    def test_menu_bar_structure_initialized(self) -> None:
        menubar = self.window.menuBar()
        menus = [action.text() for action in menubar.actions()]
        self.assertIn("&File", menus)
        self.assertIn("&Tools", menus)
        self.assertIn("&Help", menus)

    def test_saving_archived_formula_prompts_user_before_restoration(self) -> None:
        self.repo.delete_recipe(self.recipe.id)
        self.assertIsNone(self.repo.get_recipe(self.recipe.id, include_archived=False))
        self.assertIsNotNone(self.repo.get_recipe(self.recipe.id, include_archived=True))

        self.window._load_selected_recipe(self.recipe.id)

        # 1. User cancels prompt -> formula remains archived
        self.mock_question.return_value = QMessageBox.StandardButton.No
        result = self.window._save_current_recipe()
        self.assertFalse(result)
        self.assertIsNone(self.repo.get_recipe(self.recipe.id, include_archived=False))

        # 2. User confirms prompt -> formula is restored to active library
        self.mock_question.return_value = QMessageBox.StandardButton.Yes
        result = self.window._save_current_recipe()
        self.assertTrue(result)
        self.assertIsNotNone(self.repo.get_recipe(self.recipe.id, include_archived=False))

    def test_unsaved_new_draft_prompts_on_navigation_and_close(self) -> None:
        # Create a new draft without saving
        self.window._create_new_recipe()
        self.assertIsNone(self.window._current_recipe_id)

        # Modify draft
        self.window.name_input.setText("Fresh Unsaved Formulation")
        self.assertTrue(self.window._is_dirty)

        # 1. Navigation prompt triggers and Discard allows navigating
        self.mock_question.return_value = QMessageBox.StandardButton.Discard
        self.window._on_recipe_selected(0)
        self.assertFalse(self.window._is_dirty)

        # 2. Re-create new draft and test closeEvent prompt
        self.window._create_new_recipe()
        self.window.name_input.setText("Second Unsaved Formulation")
        self.assertTrue(self.window._is_dirty)

        self.mock_question.return_value = QMessageBox.StandardButton.Cancel
        cancel_evt = QCloseEvent()
        self.window.closeEvent(cancel_evt)
        self.assertFalse(cancel_evt.isAccepted())
        self.assertTrue(self.window._is_dirty)

        self.mock_question.return_value = QMessageBox.StandardButton.Discard
        discard_evt = QCloseEvent()
        self.window.closeEvent(discard_evt)
        self.assertTrue(discard_evt.isAccepted())

    def test_empty_ingredient_name_renders_error_styling(self) -> None:
        self.window.table_model.add_ingredient(name="   ", percentage=Decimal("100.00"))
        # Must be styled as error red, not empty-neutral gray
        style = self.window.validation_label.styleSheet()
        self.assertIn("#842029", style)

    def test_duplicate_selected_recipe_creates_new_v1_recipe(self) -> None:
        initial_count = len(self.repo.list_recipes())
        self.window._duplicate_selected_recipe(self.recipe)

        recipes = self.repo.list_recipes()
        self.assertEqual(len(recipes), initial_count + 1)

        dupe = [r for r in recipes if r.id != self.recipe.id][0]
        self.assertEqual(dupe.name, f"{self.recipe.name} (Copy)")
        self.assertEqual(dupe.version, 1)
        self.assertEqual(len(dupe.ingredients), len(self.recipe.ingredients))

    def test_auto_backup_disabled_by_default_creates_no_file_on_close(self) -> None:
        backup_dir = Path(self.temp_dir.name) / "cloud_sync"
        backup_dir.mkdir()
        self.config.auto_backup_dir = str(backup_dir)
        # auto_backup_enabled remains False by default
        self.assertFalse(self.config.auto_backup_enabled)

        event = QCloseEvent()
        self.window.closeEvent(event)
        self.assertTrue(event.isAccepted())

        backup_file = backup_dir / "tared_workspace.json"
        self.assertFalse(backup_file.exists())

    def test_auto_backup_enabled_creates_workspace_file_on_close(self) -> None:
        backup_dir = Path(self.temp_dir.name) / "cloud_sync_enabled"
        backup_dir.mkdir()
        self.config.auto_backup_enabled = True
        self.config.auto_backup_dir = str(backup_dir)
        self.assertTrue(self.config.is_backup_configured())

        event = QCloseEvent()
        self.window.closeEvent(event)
        self.assertTrue(event.isAccepted())

        backup_file = backup_dir / "tared_workspace.json"
        self.assertTrue(backup_file.is_file())

        content = json.loads(backup_file.read_text(encoding="utf-8"))
        self.assertEqual(content["schema_version"], "1.0")
        self.assertEqual(len(content["recipes"]), 1)


if __name__ == "__main__":
    unittest.main()
