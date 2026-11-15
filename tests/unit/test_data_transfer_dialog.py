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
        self.assertEqual(self.dialog.btn_export_selected.text(), "Export Selected Formulas...")

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

    def test_selective_multi_export_disambiguates_preexisting_disk_files(self) -> None:
        from tared.core.models import Ingredient, Recipe

        r2 = Recipe(
            id="r-multi-pre", name="Glaze Extra", version=1, ingredients=(Ingredient("Water", Decimal("100.00")),)
        )
        self.repo.save_recipe(r2)

        dlg = DataTransferDialog(self.repo, self.service)
        self.addCleanup(dlg.close)
        dlg._select_all_recipes()

        out_dir = Path(self.temp_dir.name) / "multi_preexist"
        out_dir.mkdir()
        sentinel_file = out_dir / "Standard_Benchmark_Formulation_v1.json"
        sentinel_file.write_text('{"PRESERVE": true}', encoding="utf-8")

        with patch("PyQt6.QtWidgets.QFileDialog.getExistingDirectory", return_value=str(out_dir)):
            with patch("PyQt6.QtWidgets.QMessageBox.information"):
                dlg._export_selected_recipes()

        self.assertEqual(sentinel_file.read_text(encoding="utf-8"), '{"PRESERVE": true}')
        disambiguated = list(out_dir.glob("Standard_Benchmark_Formulation_v1_*.json"))
        self.assertEqual(len(disambiguated), 1)

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

    def test_import_recipes_escapes_html_in_collision_prompt(self) -> None:
        from tared.core.models import Ingredient, Recipe
        from tared.storage.json_repo import save_recipe_to_file

        tricky = Recipe(
            id=self.recipe.id,
            name="Acid <10%> & Base",
            ingredients=(Ingredient("Water", Decimal("100.00")),),
        )
        json_file = Path(self.temp_dir.name) / "tricky_collision.json"
        save_recipe_to_file(tricky, json_file)

        with patch(
            "PyQt6.QtWidgets.QFileDialog.getOpenFileNames",
            return_value=([str(json_file)], "JSON Files (*.json)"),
        ):
            with patch(
                "PyQt6.QtWidgets.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Cancel,
            ) as mock_q:
                self.dialog._import_recipes()
                mock_q.assert_called_once()
                prompt_text = mock_q.call_args[0][2]
                self.assertIn("Acid &lt;10%&gt; &amp; Base", prompt_text)
                self.assertNotIn("<10%>", prompt_text)

    def test_selective_multi_recipe_export_disambiguates_colliding_filenames(self) -> None:
        from PyQt6.QtCore import Qt

        from tared.core.models import Ingredient, Recipe
        from tared.storage.json_repo import load_recipe_from_file

        r1 = Recipe(
            id="collide-aaa11111",
            name="Glaze A/B",
            version=1,
            ingredients=(Ingredient("Water", Decimal("100.00")),),
        )
        r2 = Recipe(
            id="collide-bbb22222",
            name="Glaze A:B",
            version=1,
            ingredients=(Ingredient("Sugar", Decimal("100.00")),),
        )
        self.repo.save_recipe(r1)
        self.repo.save_recipe(r2)

        dlg = DataTransferDialog(self.repo, self.service)
        self.addCleanup(dlg.close)

        for i in range(dlg.recipe_list.count()):
            item = dlg.recipe_list.item(i)
            rec = item.data(Qt.ItemDataRole.UserRole)
            if rec.id in (r1.id, r2.id):
                item.setCheckState(Qt.CheckState.Checked)

        out_dir = Path(self.temp_dir.name) / "multi_export"
        out_dir.mkdir()
        with patch("PyQt6.QtWidgets.QFileDialog.getExistingDirectory", return_value=str(out_dir)):
            with patch("PyQt6.QtWidgets.QMessageBox.information"):
                dlg._export_selected_recipes()

        exported = sorted(out_dir.glob("*.json"))
        self.assertEqual(len(exported), 2)
        loaded_ids = {load_recipe_from_file(p).id for p in exported}
        self.assertEqual(loaded_ids, {r1.id, r2.id})

    def test_selective_multi_recipe_export_avoids_cross_branch_filename_overwrite(self) -> None:
        from PyQt6.QtCore import Qt

        from tared.core.models import Ingredient, Recipe
        from tared.storage.json_repo import load_recipe_from_file

        r1 = Recipe(
            id="1111aaaa-0000",
            name="Glaze A",
            version=1,
            ingredients=(Ingredient("Water", Decimal("100.00")),),
        )
        r2 = Recipe(
            id="2222bbbb-0000",
            name="Glaze A",
            version=1,
            ingredients=(Ingredient("Sugar", Decimal("100.00")),),
        )
        # Sanitized single name matches r1's disambiguated filename (Glaze_A_v1_1111aaaa.json)
        r3 = Recipe(
            id="3333cccc-0000",
            name="Glaze A v1 1111aaaa",
            version=1,
            ingredients=(Ingredient("Salt", Decimal("100.00")),),
        )
        for r in (r1, r2, r3):
            self.repo.save_recipe(r)

        # Normalize r3 name in recipe_list check by creating exact collision with r1's base+short_id
        self.repo.delete_recipe(r3.id)
        r3_exact = Recipe(
            id="3333cccc-0000",
            name="Glaze A_v1_1111aaaa",
            version=1,
            ingredients=(Ingredient("Salt", Decimal("100.00")),),
        )
        # With version=1, base is Glaze_A_v1_1111aaaa_v1; instead collide with r1_base + _short_id:
        r3_collide = Recipe(
            id="3333cccc-0000",
            name="Glaze A v1 1111aaaa",
            version=1,
            ingredients=(Ingredient("Salt", Decimal("100.00")),),
        )
        del r3_exact, r3_collide

        # Directly verify r1, r2, and a recipe whose "<clean_name>_v<ver>" equals r1's disambiguated stem
        r4 = Recipe(
            id="4444dddd-0000",
            name="Glaze A v1",
            version=1111,
            ingredients=(Ingredient("Oil", Decimal("100.00")),),
        )
        # Simpler exact stem match: name="Glaze A_v1_1111aaaa" at version=1 wouldn't match _v1,
        # whereas name="Glaze A", version=1 -> base="Glaze_A_v1", disambiguated="Glaze_A_v1_1111aaaa".
        # So a recipe with name="Glaze_A_v1_1111aaaa" won't match unless version is stripped;
        # use name="Glaze_A" (1) + "Glaze_A" (1) and name="Glaze A_v1_1111aaaa" isn't needed—let's test versioned:
        # name="Glaze A" (v1, id="aaaaaaaa") -> "Glaze_A_v1_aaaaaaaa"
        # name="Glaze A v1" -> if version=1? No, version must be int; e.g. name="Glaze A_v1_aaaaaaaa" -> ends with _v1?
        # Wait: name="Glaze A v1", id="aaaaaaaa", vs name="Glaze A v1 aaaaaaaa"? No, suffix is _v<version>.
        # What if r_a has name="Glaze A", version=1, id="1111aaaa" -> "Glaze_A_v1_1111aaaa.json"
        # and r_b has name="Glaze A", version=1, id="2222bbbb" -> "Glaze_A_v1_2222bbbb.json"
        # and r_c has name="Glaze A v1 1111aaaa", version=1 -> "Glaze_A_v1_1111aaaa_v1.json" (different).
        # To truly collide across branches: r_a has name="Mix", version=1, id="v1" -> "Mix_v1_v1.json",
        # and r_c has name="Mix v1", version=1 -> "Mix_v1_v1.json"!
        self.repo.delete_recipe(r4.id)
        ra = Recipe(id="v1", name="Mix", version=1, ingredients=(Ingredient("A", Decimal("100.00")),))
        rb = Recipe(id="v2", name="Mix", version=1, ingredients=(Ingredient("B", Decimal("100.00")),))
        rc = Recipe(id="unique-id", name="Mix v1", version=1, ingredients=(Ingredient("C", Decimal("100.00")),))
        for r in (ra, rb, rc):
            self.repo.save_recipe(r)

        dlg = DataTransferDialog(self.repo, self.service)
        self.addCleanup(dlg.close)
        for i in range(dlg.recipe_list.count()):
            item = dlg.recipe_list.item(i)
            rec = item.data(Qt.ItemDataRole.UserRole)
            if rec.id in (ra.id, rb.id, rc.id):
                item.setCheckState(Qt.CheckState.Checked)

        out_dir = Path(self.temp_dir.name) / "cross_branch"
        out_dir.mkdir()
        with patch("PyQt6.QtWidgets.QFileDialog.getExistingDirectory", return_value=str(out_dir)):
            with patch("PyQt6.QtWidgets.QMessageBox.information"):
                dlg._export_selected_recipes()

        exported = sorted(out_dir.glob("*.json"))
        self.assertEqual(len(exported), 3)
        self.assertEqual({load_recipe_from_file(p).id for p in exported}, {ra.id, rb.id, rc.id})

    def test_import_recipes_overwrites_corrupted_db_record_cleanly(self) -> None:
        from tared.core.models import Ingredient, Recipe
        from tared.storage.json_repo import save_recipe_to_file

        conn = self.repo._get_connection()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO recipes (id, name, version, updated_at, is_archived) "
                    "VALUES ('corrupt-imp-1', 'Corrupt Target', 1, '', 0);"
                )
        finally:
            conn.close()

        incoming = Recipe(
            id="corrupt-imp-1",
            name="Repaired Imported Formula",
            version=1,
            ingredients=(Ingredient("Water", Decimal("100.00")),),
        )
        json_file = Path(self.temp_dir.name) / "repair_import.json"
        save_recipe_to_file(incoming, json_file)

        with patch(
            "PyQt6.QtWidgets.QFileDialog.getOpenFileNames",
            return_value=([str(json_file)], "JSON Files (*.json)"),
        ):
            with patch("PyQt6.QtWidgets.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
                with patch("PyQt6.QtWidgets.QMessageBox.information"):
                    with patch("PyQt6.QtWidgets.QMessageBox.critical") as mock_crit:
                        self.dialog._import_recipes()
                        mock_crit.assert_not_called()

        restored = self.repo.get_recipe("corrupt-imp-1")
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.name, "Repaired Imported Formula")
        self.assertEqual(restored.version, 2)

    def test_populate_recipes_marks_draft_and_import_triggers_auto_backup(self) -> None:
        from PyQt6.QtCore import QSettings

        from tared.config import ConfigManager
        from tared.core.models import Ingredient, Recipe
        from tared.storage.json_repo import save_recipe_to_file

        draft = Recipe(
            id="dt-draft-01",
            name="Transfer Draft",
            version=1,
            is_draft=True,
            ingredients=(Ingredient("Water", Decimal("50.00")),),
        )
        self.repo.save_recipe(draft)

        backup_dir = Path(self.temp_dir.name) / "dt_backups"
        backup_dir.mkdir()
        ini_path = Path(self.temp_dir.name) / "dt_cfg.ini"
        cfg = ConfigManager(QSettings(str(ini_path), QSettings.Format.IniFormat))
        cfg.auto_backup_enabled = True
        cfg.auto_backup_dir = str(backup_dir)

        dlg = DataTransferDialog(self.repo, self.service, config=cfg)
        self.addCleanup(dlg.close)
        labels = [dlg.recipe_list.item(i).text() for i in range(dlg.recipe_list.count())]
        self.assertTrue(any("Transfer Draft (v1 Draft)" == lbl for lbl in labels))

        cat_file = Path(self.temp_dir.name) / "backup_cat.json"
        cat_file.write_text(
            json.dumps({"schema_version": "1.0", "catalog": [{"name": "Citric Acid", "cost_per_unit": "4.50"}]}),
            encoding="utf-8",
        )
        with patch("PyQt6.QtWidgets.QFileDialog.getOpenFileName", return_value=(str(cat_file), "JSON Files (*.json)")):
            with patch("PyQt6.QtWidgets.QMessageBox.information"):
                dlg._import_catalog()

        backup_file = backup_dir / "tared_workspace.json"
        self.assertTrue(backup_file.is_file())

        imp_file = Path(self.temp_dir.name) / "imp_extra.json"
        save_recipe_to_file(
            Recipe(id="dt-imp-02", name="Backup Triggered Formula", ingredients=(Ingredient("A", Decimal("100.00")),)),
            imp_file,
        )
        with patch(
            "PyQt6.QtWidgets.QFileDialog.getOpenFileNames",
            return_value=([str(imp_file)], "JSON Files (*.json)"),
        ):
            with patch("PyQt6.QtWidgets.QMessageBox.information"):
                dlg._import_recipes()

        content = json.loads(backup_file.read_text(encoding="utf-8"))
        self.assertIn("Backup Triggered Formula", [r["name"] for r in content["recipes"]])

    def test_reset_workspace_preserves_existing_cloud_backup_file(self) -> None:
        from PyQt6.QtCore import QSettings

        from tared.config import ConfigManager

        backup_dir = Path(self.temp_dir.name) / "cloud_keep"
        backup_dir.mkdir()
        cloud_file = backup_dir / "tared_workspace.json"
        sentinel_payload = json.dumps({"schema_version": "1.0", "recipes": [{"name": "Keep Me"}]})
        cloud_file.write_text(sentinel_payload, encoding="utf-8")

        ini_path = Path(self.temp_dir.name) / "cloud_keep.ini"
        cfg = ConfigManager(QSettings(str(ini_path), QSettings.Format.IniFormat))
        cfg.auto_backup_enabled = True
        cfg.auto_backup_dir = str(backup_dir)

        dlg = DataTransferDialog(self.repo, self.service, config=cfg)
        self.addCleanup(dlg.close)

        with patch("PyQt6.QtWidgets.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            with patch("PyQt6.QtWidgets.QInputDialog.getText", return_value=("RESET", True)):
                with patch("PyQt6.QtWidgets.QMessageBox.information"):
                    dlg._reset_workspace()

        self.assertEqual(len(self.repo.list_recipes()), 0)
        self.assertEqual(cloud_file.read_text(encoding="utf-8"), sentinel_payload)

    def test_json_exports_append_json_extension_preserving_dots(self) -> None:
        ws_base = Path(self.temp_dir.name) / "ws_v1.2"
        with patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName", return_value=(str(ws_base), "JSON Files (*.json)")):
            with patch("PyQt6.QtWidgets.QMessageBox.information"):
                self.dialog._export_workspace()
        self.assertTrue(Path(f"{ws_base}.json").is_file())

        cat_base = Path(self.temp_dir.name) / "cat_v1.2"
        with patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName", return_value=(str(cat_base), "JSON Files (*.json)")):
            with patch("PyQt6.QtWidgets.QMessageBox.information"):
                self.dialog._export_catalog()
        self.assertTrue(Path(f"{cat_base}.json").is_file())

        self.dialog._select_all_recipes()
        rcp_base = Path(self.temp_dir.name) / "rcp_v1.2"
        with patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName", return_value=(str(rcp_base), "JSON Files (*.json)")):
            with patch("PyQt6.QtWidgets.QMessageBox.information"):
                self.dialog._export_selected_recipes()
        self.assertTrue(Path(f"{rcp_base}.json").is_file())


if __name__ == "__main__":
    unittest.main()
