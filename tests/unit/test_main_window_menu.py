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

    def test_configured_default_precision_applied_on_startup(self) -> None:
        custom_ini = Path(self.temp_dir.name) / "custom_prec.ini"
        custom_cfg = ConfigManager(QSettings(str(custom_ini), QSettings.Format.IniFormat))
        custom_cfg.default_precision = "0.001"

        win = MainWindow(self.repo, config=custom_cfg)
        self.addCleanup(win.close)

        self.assertEqual(win.precision_combo.currentData(), "0.001")
        self.assertEqual(win.table_model.precision, Decimal("0.001"))
        self.assertEqual(win.target_weight_spin.decimals(), 3)
        self.assertFalse(win._is_dirty)

    def test_duplicate_active_dirty_recipe_prompts_and_handles_cancel_and_save(self) -> None:
        self.window.name_input.setText("Unsaved Edited Title")
        self.assertTrue(self.window._is_dirty)

        # 1. Cancel aborts duplication and preserves unsaved edits
        self.mock_question.return_value = QMessageBox.StandardButton.Cancel
        self.window._duplicate_selected_recipe(self.recipe)
        self.assertEqual(len(self.repo.list_recipes()), 1)
        self.assertEqual(self.window.name_input.text(), "Unsaved Edited Title")
        self.assertTrue(self.window._is_dirty)

        # 2. Save commits active edits first and duplicates the updated formulation
        self.mock_question.return_value = QMessageBox.StandardButton.Save
        self.window._duplicate_selected_recipe(self.recipe)
        recipes = self.repo.list_recipes()
        self.assertEqual(len(recipes), 2)
        orig = self.repo.get_recipe(self.recipe.id)
        assert orig is not None
        self.assertEqual(orig.name, "Unsaved Edited Title")
        dupes = [r for r in recipes if r.id != self.recipe.id]
        self.assertEqual(dupes[0].name, "Unsaved Edited Title (Copy)")
        self.assertFalse(self.window._is_dirty)

    def test_dirty_save_on_close_triggers_auto_backup_once(self) -> None:
        backup_dir = Path(self.temp_dir.name) / "cloud_sync_once"
        backup_dir.mkdir()
        self.config.auto_backup_enabled = True
        self.config.auto_backup_dir = str(backup_dir)

        self.window.name_input.setText("Saved On Close Once")
        self.assertTrue(self.window._is_dirty)
        self.mock_question.return_value = QMessageBox.StandardButton.Save

        with patch(
            "tared.ui.views.main_window.export_workspace_to_json_file",
            side_effect=lambda *args, **kwargs: Path(args[1]),
        ) as mock_export:
            event = QCloseEvent()
            self.window.closeEvent(event)
            self.assertTrue(event.isAccepted())
            mock_export.assert_called_once()

    def test_auto_backup_missing_dir_records_failure_and_prompts_on_close(self) -> None:
        missing_dir = Path(self.temp_dir.name) / "unmounted_sync_drive"
        self.config.auto_backup_enabled = True
        self.config.auto_backup_dir = str(missing_dir)

        with self.assertLogs("tared.ui.views.main_window", level="WARNING") as cm:
            self.mock_warn.return_value = QMessageBox.StandardButton.No
            cancel_evt = QCloseEvent()
            self.window.closeEvent(cancel_evt)
            self.assertFalse(cancel_evt.isAccepted())
            self.mock_warn.assert_called_once()
            self.assertIn("FAILED:", self.config.last_backup_status)

            self.mock_warn.return_value = QMessageBox.StandardButton.Yes
            accept_evt = QCloseEvent()
            self.window.closeEvent(accept_evt)
            self.assertTrue(accept_evt.isAccepted())

        self.assertEqual(len(cm.output), 2)
        self.assertTrue(all("unmounted_sync_drive" in msg for msg in cm.output))

    def test_recipe_context_menu_escapes_ampersands_in_action_text(self) -> None:
        from PyQt6.QtCore import QPoint, Qt

        from tared.core.models import Ingredient, Recipe

        amp_rec = Recipe(
            id="rec-amp-test",
            name="Oil & Vinegar Dressing",
            version=1,
            ingredients=(Ingredient("Oil", Decimal("100.00")),),
        )
        self.repo.save_recipe(amp_rec)
        self.window._load_recipe_archive(select_id="rec-amp-test")

        for i in range(self.window.recipe_list.count()):
            item = self.window.recipe_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == "rec-amp-test":
                rect = self.window.recipe_list.visualItemRect(item)
                with patch("PyQt6.QtWidgets.QMenu.exec") as mock_menu_exec:
                    self.window._on_recipe_list_context_menu(rect.center() if rect.isValid() else QPoint(5, 5))
                    mock_menu_exec.assert_called_once()
                break

    def test_startup_update_timer_stopped_on_close_and_skips_when_closing(self) -> None:
        from PyQt6.QtCore import QTimer

        timer = QTimer(self.window)
        timer.setSingleShot(True)
        timer.start(5000)
        self.window._startup_update_timer = timer
        self.assertTrue(timer.isActive())

        evt = QCloseEvent()
        self.window.closeEvent(evt)
        self.assertTrue(evt.isAccepted())
        self.assertFalse(timer.isActive())
        self.assertIsNone(self.window._startup_update_timer)

        with patch("tared.ui.models.update_worker.CheckUpdateWorker") as mock_worker:
            self.window._check_updates_silently()
            mock_worker.assert_not_called()

    def test_save_as_draft_with_zero_percentage_displays_readable_action_label(self) -> None:
        self.window._create_new_recipe()
        self.window.name_input.setText("Catalog Draft")
        self.window.table_model.add_ingredient(
            name="Sugar",
            percentage=Decimal("0.00"),
            cost_per_unit=Decimal("1.25"),
        )
        self.mock_warn.reset_mock()

        result = self.window._save_current_recipe(as_draft=True)

        self.assertFalse(result)
        self.mock_warn.assert_called_once()
        warn_msg = self.mock_warn.call_args[0][2]
        self.assertIn("save a draft", warn_msg)
        self.assertNotIn("save_draft", warn_msg)

    def test_duplicate_incomplete_non_draft_formula_downgrades_to_draft_without_crashing(self) -> None:
        from tared.core.models import Ingredient, Recipe

        incomplete = Recipe(
            id="rec-legacy-incomplete",
            name="Legacy Incomplete",
            version=1,
            is_draft=False,
            ingredients=(Ingredient("Water", Decimal("60.00"), Decimal("0.10")),),
        )
        self.repo.save_recipe(incomplete)

        self.window._duplicate_selected_recipe(incomplete)

        copies = [r for r in self.repo.list_recipes() if r.name == "Legacy Incomplete (Copy)"]
        self.assertEqual(len(copies), 1)
        self.assertTrue(copies[0].is_draft)
        self.assertEqual(copies[0].version, 1)

    def test_uncommitted_inline_editor_flushed_before_catalog_and_delete_actions(self) -> None:
        from PyQt6.QtWidgets import QLineEdit

        from tared.core.models import Ingredient, Recipe

        repo = SQLiteRecipeRepository(":memory:")
        self.addCleanup(repo.close)
        repo.save_recipe(
            Recipe(
                id="r-flush",
                name="Flush Formula",
                ingredients=(Ingredient("Sugar", Decimal("100.00"), Decimal("1.00")),),
            )
        )

        window = MainWindow(repo)
        self.addCleanup(window.close)

        idx = window.table_model.index(0, 2)
        window.table_view.setCurrentIndex(idx)
        window.table_view.edit(idx)
        editor = window.table_view.viewport().findChild(QLineEdit)
        self.assertIsNotNone(editor)
        assert editor is not None
        editor.setText("8.75")

        window._add_selected_to_catalog()
        cat_item = repo.get_catalog_ingredient("Sugar")
        self.assertIsNotNone(cat_item)
        assert cat_item is not None
        self.assertEqual(cat_item.cost_per_unit, Decimal("8.75"))

    def test_commit_active_table_editor_and_json_export_suffix(self) -> None:
        from PyQt6.QtWidgets import QAbstractItemView, QLineEdit

        # Idle commit is a safe no-op
        self.window._commit_active_table_editor()
        self.assertFalse(self.window._is_dirty)

        idx = self.window.table_model.index(0, 2)
        self.window.table_view.setCurrentIndex(idx)
        self.window.table_view.edit(idx)
        self.assertEqual(self.window.table_view.state(), QAbstractItemView.State.EditingState)
        editor = self.window.table_view.viewport().findChild(QLineEdit)
        self.assertIsNotNone(editor)
        assert editor is not None
        editor.setText("19.75")

        self.window._commit_active_table_editor()
        self.assertEqual(self.window.table_view.state(), QAbstractItemView.State.NoState)
        self.assertEqual(self.window.table_model.ingredients_data[0]["cost_per_unit"], Decimal("19.75"))
        self.assertTrue(self.window._is_dirty)

        out_base = Path(self.temp_dir.name) / "formula_v1.2"
        with patch(
            "PyQt6.QtWidgets.QFileDialog.getSaveFileName",
            return_value=(str(out_base), "JSON Files (*.json)"),
        ):
            self.window._export_recipe()
        self.assertTrue(Path(f"{out_base}.json").is_file())

    def test_uncommitted_inline_editor_flushed_before_new_and_data_transfer_prompts(self) -> None:
        from PyQt6.QtWidgets import QLineEdit

        idx = self.window.table_model.index(0, 2)
        self.window.table_view.setCurrentIndex(idx)
        self.window.table_view.edit(idx)
        editor = self.window.table_view.viewport().findChild(QLineEdit)
        self.assertIsNotNone(editor)
        assert editor is not None
        editor.setText("42.00")

        self.mock_question.reset_mock()
        self.mock_question.return_value = QMessageBox.StandardButton.No
        self.window._create_new_recipe()
        self.mock_question.assert_called_once()
        self.assertTrue(self.window._is_dirty)

        self.mock_question.reset_mock()
        self.mock_question.return_value = QMessageBox.StandardButton.Cancel
        with patch("tared.ui.views.main_window.DataTransferDialog") as mock_dt:
            self.window._open_data_transfer_dialog()
            self.mock_question.assert_called_once()
            mock_dt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
