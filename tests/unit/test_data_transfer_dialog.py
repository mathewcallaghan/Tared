"""Unit tests verifying DataTransferDialog catalog import/export, workspace operations, and selective exports."""

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication, QMessageBox

from tared.core.models import CatalogIngredient
from tared.core.service import RecipeService
from tared.storage.sqlite_repo import SQLiteRecipeRepository
from tared.ui.views.data_transfer_dialog import DataTransferDialog
from tests.conftest import create_test_recipe


class TestDataTransferDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if QApplication.instance() is None:
            cls.app = QApplication(["test-transfer-runner"])
        else:
            cls.app = QApplication.instance()

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_dt.db"
        self.repo = SQLiteRecipeRepository(self.db_path)
        self.service = RecipeService(self.repo)

        # Seed data
        self.recipe = create_test_recipe()
        self.repo.save_recipe(self.recipe)
        self.repo.save_catalog_ingredient(CatalogIngredient("Sugar", Decimal("1.25"), supplier="Refinery A"))

        self.dialog = DataTransferDialog(self.repo, self.service)

    def tearDown(self) -> None:
        self.dialog.close()
        self.repo.close()
        self.temp_dir.cleanup()

    def test_recipe_list_population_and_selection_count(self) -> None:
        self.assertEqual(self.dialog.recipe_list.count(), 1)
        self.assertFalse(self.dialog.btn_export_selected.isEnabled())

        self.dialog._select_all_recipes()
        self.assertTrue(self.dialog.btn_export_selected.isEnabled())
        self.assertIn("Export Selected (1)", self.dialog.btn_export_selected.text())

        self.dialog._clear_recipe_selection()
        self.assertFalse(self.dialog.btn_export_selected.isEnabled())

    def test_export_catalog_action(self) -> None:
        out_json = str(Path(self.temp_dir.name) / "catalog_export.json")
        with patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName", return_value=(out_json, "JSON Files (*.json)")):
            with patch("PyQt6.QtWidgets.QMessageBox.information") as mock_info:
                self.dialog._export_catalog()
                mock_info.assert_called_once()

        self.assertTrue(Path(out_json).is_file())
        content = json.loads(Path(out_json).read_text(encoding="utf-8"))
        self.assertEqual(len(content["catalog"]), 1)
        self.assertEqual(content["catalog"][0]["name"], "Sugar")

    def test_import_catalog_action(self) -> None:
        cat_file = Path(self.temp_dir.name) / "incoming_catalog.json"
        cat_payload = {
            "schema_version": "1.0",
            "catalog": [{"name": "Pectin", "cost_per_unit": "18.50", "supplier": "Fruit Puree Ltd"}],
        }
        cat_file.write_text(json.dumps(cat_payload), encoding="utf-8")

        signals_received: list[bool] = []
        self.dialog.catalog_imported.connect(lambda: signals_received.append(True))

        with patch("PyQt6.QtWidgets.QFileDialog.getOpenFileName", return_value=(str(cat_file), "JSON Files (*.json)")):
            with patch("PyQt6.QtWidgets.QMessageBox.information"):
                self.dialog._import_catalog()

        self.assertEqual(len(signals_received), 1)
        item = self.repo.get_catalog_ingredient("Pectin")
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.cost_per_unit, Decimal("18.50"))

    def test_selective_recipe_export(self) -> None:
        self.dialog._select_all_recipes()
        out_single = str(Path(self.temp_dir.name) / "single_recipe.json")

        with patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName", return_value=(out_single, "JSON Files (*.json)")):
            with patch("PyQt6.QtWidgets.QMessageBox.information") as mock_info:
                self.dialog._export_selected_recipes()
                mock_info.assert_called_once()

        self.assertTrue(Path(out_single).is_file())
        content = json.loads(Path(out_single).read_text(encoding="utf-8"))
        self.assertEqual(content["name"], self.recipe.name)

    def test_reset_workspace_action_clears_and_emits_restored(self) -> None:
        restored_events: list[bool] = []
        self.dialog.workspace_restored.connect(lambda: restored_events.append(True))

        with patch("PyQt6.QtWidgets.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            with patch("PyQt6.QtWidgets.QInputDialog.getText", return_value=("RESET", True)):
                with patch("PyQt6.QtWidgets.QMessageBox.information"):
                    with patch("PyQt6.QtWidgets.QMessageBox.critical"):
                        self.dialog._reset_workspace()

        self.assertEqual(len(restored_events), 1)
        self.assertEqual(len(self.repo.list_recipes()), 0)
        self.assertEqual(len(self.repo.list_catalog_ingredients()), 0)

    def test_reset_workspace_aborts_when_reset_keyword_is_not_typed(self) -> None:
        restored_events: list[bool] = []
        self.dialog.workspace_restored.connect(lambda: restored_events.append(True))

        with patch("PyQt6.QtWidgets.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            with patch("PyQt6.QtWidgets.QInputDialog.getText", return_value=("wrong-word", True)):
                with patch("PyQt6.QtWidgets.QMessageBox.information"):
                    self.dialog._reset_workspace()

        self.assertEqual(len(restored_events), 0)
        self.assertGreater(len(self.repo.list_recipes()), 0)


if __name__ == "__main__":
    unittest.main()
