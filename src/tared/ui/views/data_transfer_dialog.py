"""Centralized data transfer dialog for workspace backup, selective export, and multi-file imports."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from tared.config import ConfigManager
from tared.core.models import Recipe
from tared.core.service import RecipeService
from tared.storage.json_repo import load_recipe_from_file, save_recipe_to_file
from tared.storage.sqlite_repo import SQLiteRecipeRepository
from tared.storage.workspace_io import (
    export_catalog_to_json_file,
    export_workspace_to_json_file,
    import_catalog_from_json_file,
    inspect_workspace_file,
    restore_workspace_from_json,
)


class DataTransferDialog(QDialog):
    """Modal dialog providing comprehensive export and import options."""

    workspace_restored = pyqtSignal()
    recipes_imported = pyqtSignal()
    catalog_imported = pyqtSignal()

    def __init__(
        self,
        repository: SQLiteRecipeRepository,
        service: RecipeService,
        config: ConfigManager | None = None,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.repo = repository
        self.service = service
        self.config = config or ConfigManager()
        self.setWindowTitle("Data Transfer & Backup")
        self.resize(680, 520)
        self.setMinimumWidth(560)

        self._all_recipes: list[Recipe] = []
        self._setup_ui()
        self._populate_recipes()

    def _setup_ui(self) -> None:
        main_layout = QVBoxLayout(self)

        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)

        # Tab 1: Export Data
        export_tab = QWidget()
        export_layout = QVBoxLayout(export_tab)
        export_layout.setSpacing(14)

        # 1. Full Workspace Export
        ws_box = QGroupBox("Full Workspace Snapshot (Failsafe Backup)")
        ws_layout = QVBoxLayout(ws_box)
        ws_info = QLabel(
            "Export all active and archived recipes, revisions, batch records, "
            "and the raw materials catalog into a single portable JSON file."
        )
        ws_info.setWordWrap(True)
        self.btn_export_workspace = QPushButton("Export Entire Workspace...")
        self.btn_export_workspace.setStyleSheet("font-weight: bold; padding: 6px 14px;")
        ws_layout.addWidget(ws_info)
        ws_layout.addWidget(self.btn_export_workspace, alignment=Qt.AlignmentFlag.AlignLeft)
        export_layout.addWidget(ws_box)

        # 2. Selective Recipe Export
        sel_box = QGroupBox("Selective Formula Export")
        sel_layout = QVBoxLayout(sel_box)

        sel_search_layout = QHBoxLayout()
        self.recipe_search_input = QLineEdit()
        self.recipe_search_input.setPlaceholderText("Filter formulas...")
        self.recipe_search_input.setClearButtonEnabled(True)
        self.btn_select_all = QPushButton("Select All")
        self.btn_select_none = QPushButton("Clear")
        sel_search_layout.addWidget(self.recipe_search_input)
        sel_search_layout.addWidget(self.btn_select_all)
        sel_search_layout.addWidget(self.btn_select_none)
        sel_layout.addLayout(sel_search_layout)

        self.recipe_list = QListWidget()
        self.recipe_list.setMaximumHeight(150)
        sel_layout.addWidget(self.recipe_list)

        self.btn_export_selected = QPushButton("Export Selected Formulas...")
        self.btn_export_selected.setStyleSheet("padding: 5px 12px;")
        self.btn_export_selected.setEnabled(False)
        sel_layout.addWidget(self.btn_export_selected, alignment=Qt.AlignmentFlag.AlignLeft)
        export_layout.addWidget(sel_box)

        # 3. Catalog Export
        cat_box = QGroupBox("Raw Materials Catalog")
        cat_layout = QVBoxLayout(cat_box)
        cat_info = QLabel(
            "Export the current raw materials price-book and supplier metadata as a standalone JSON file."
        )
        self.btn_export_catalog = QPushButton("Export Catalog JSON...")
        cat_layout.addWidget(cat_info)
        cat_layout.addWidget(self.btn_export_catalog, alignment=Qt.AlignmentFlag.AlignLeft)
        export_layout.addWidget(cat_box)

        self.tabs.addTab(export_tab, "Export Data")

        # Tab 2: Import & Restore (literal && prevents mnemonic underscore)
        import_tab = QWidget()
        import_layout = QVBoxLayout(import_tab)
        import_layout.setSpacing(14)

        # 1. Restore Workspace
        rst_box = QGroupBox("Restore Full Workspace")
        rst_layout = QVBoxLayout(rst_box)
        rst_info = QLabel(
            "Restore your entire formulation environment from a workspace backup JSON. "
            "A safety snapshot of your current database will be saved before restoring."
        )
        rst_info.setWordWrap(True)
        self.btn_restore_workspace = QPushButton("Restore Workspace from Backup...")
        self.btn_restore_workspace.setStyleSheet("font-weight: bold; color: #dc3545; padding: 6px 14px;")
        rst_layout.addWidget(rst_info)
        rst_layout.addWidget(self.btn_restore_workspace, alignment=Qt.AlignmentFlag.AlignLeft)
        import_layout.addWidget(rst_box)

        # 2. Import Recipes
        rcp_imp_box = QGroupBox("Import Formulas")
        rcp_imp_layout = QVBoxLayout(rcp_imp_box)
        rcp_imp_info = QLabel(
            "Import one or more individual formula JSON files. Collision resolution is handled per formula."
        )
        rcp_imp_info.setWordWrap(True)
        self.btn_import_recipes = QPushButton("Import Formula JSON(s)...")
        rcp_imp_layout.addWidget(rcp_imp_info)
        rcp_imp_layout.addWidget(self.btn_import_recipes, alignment=Qt.AlignmentFlag.AlignLeft)
        import_layout.addWidget(rcp_imp_box)

        # 3. Import Catalog
        cat_imp_box = QGroupBox("Import Raw Materials Catalog")
        cat_imp_layout = QVBoxLayout(cat_imp_box)
        cat_imp_info = QLabel(
            "Import raw material items and prices from a catalog JSON file into the master price book."
        )
        cat_imp_info.setWordWrap(True)
        self.btn_import_catalog = QPushButton("Import Catalog JSON...")
        cat_imp_layout.addWidget(cat_imp_info)
        cat_imp_layout.addWidget(self.btn_import_catalog, alignment=Qt.AlignmentFlag.AlignLeft)
        import_layout.addWidget(cat_imp_box)

        # 4. Reset Workspace (Danger Zone)
        reset_box = QGroupBox("Danger Zone: Reset Workspace")
        reset_box.setStyleSheet("QGroupBox { font-weight: bold; }")
        reset_layout = QVBoxLayout(reset_box)
        reset_info = QLabel(
            "Reset your entire formulation environment (active recipes, revisions, batch records, "
            "and raw materials catalog) back to a clean empty slate. "
            "An automated safety snapshot (.bak) will be saved prior to resetting."
        )
        reset_info.setWordWrap(True)
        self.btn_reset_workspace = QPushButton("Reset Workspace to Clean Slate...")
        self.btn_reset_workspace.setStyleSheet("font-weight: bold; color: #dc3545; padding: 6px 14px;")
        reset_layout.addWidget(reset_info)
        reset_layout.addWidget(self.btn_reset_workspace, alignment=Qt.AlignmentFlag.AlignLeft)
        import_layout.addWidget(reset_box)

        import_layout.addStretch()
        self.tabs.addTab(import_tab, "Import && Restore")

        # Dialog Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.btn_close = QPushButton("Close")
        self.btn_close.setStyleSheet("padding: 6px 16px;")
        btn_layout.addWidget(self.btn_close)
        main_layout.addLayout(btn_layout)

        # Connections
        self.btn_export_workspace.clicked.connect(self._export_workspace)
        self.btn_export_catalog.clicked.connect(self._export_catalog)
        self.btn_export_selected.clicked.connect(self._export_selected_recipes)
        self.btn_restore_workspace.clicked.connect(self._restore_workspace)
        self.btn_import_recipes.clicked.connect(self._import_recipes)
        self.btn_import_catalog.clicked.connect(self._import_catalog)
        self.btn_reset_workspace.clicked.connect(self._reset_workspace)
        self.btn_select_all.clicked.connect(self._select_all_recipes)
        self.btn_select_none.clicked.connect(self._clear_recipe_selection)
        self.recipe_search_input.textChanged.connect(self._filter_recipes)
        self.recipe_list.itemChanged.connect(self._on_recipe_selection_changed)
        self.btn_close.clicked.connect(self.accept)

    def _get_start_dir(self) -> str:
        return str(self.config.get_resolved_export_dir())

    def _populate_recipes(self) -> None:
        self.recipe_list.clear()
        self._all_recipes = self.repo.list_recipes()
        for r in self._all_recipes:
            item = QListWidgetItem(f"{r.name} (v{r.version})")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, r)
            self.recipe_list.addItem(item)
        self._on_recipe_selection_changed()

    def _filter_recipes(self, text: str) -> None:
        query = text.strip().lower()
        for i in range(self.recipe_list.count()):
            item = self.recipe_list.item(i)
            item.setHidden(query not in item.text().lower())

    def _select_all_recipes(self) -> None:
        for i in range(self.recipe_list.count()):
            item = self.recipe_list.item(i)
            if not item.isHidden():
                item.setCheckState(Qt.CheckState.Checked)
        self._on_recipe_selection_changed()

    def _clear_recipe_selection(self) -> None:
        for i in range(self.recipe_list.count()):
            self.recipe_list.item(i).setCheckState(Qt.CheckState.Unchecked)
        self._on_recipe_selection_changed()

    def _on_recipe_selection_changed(self) -> None:
        count = sum(
            1 for i in range(self.recipe_list.count()) if self.recipe_list.item(i).checkState() == Qt.CheckState.Checked
        )
        self.btn_export_selected.setEnabled(count > 0)
        self.btn_export_selected.setText(f"Export Selected ({count})..." if count > 0 else "Export Selected Recipes...")

    def _export_workspace(self) -> None:
        target, _ = QFileDialog.getSaveFileName(
            self,
            "Export Full Workspace Backup",
            str(Path(self._get_start_dir()) / "tared_workspace.json"),
            "JSON Files (*.json)",
        )
        if not target:
            return

        try:
            out_path = export_workspace_to_json_file(self.repo, target)
            QMessageBox.information(
                self,
                "Workspace Exported",
                f"Full workspace snapshot successfully exported to:\n{out_path}",
            )
        except Exception as err:
            QMessageBox.critical(self, "Export Error", f"Failed to export workspace:\n{err}")

    def _export_catalog(self) -> None:
        target, _ = QFileDialog.getSaveFileName(
            self,
            "Export Raw Materials Catalog",
            str(Path(self._get_start_dir()) / "raw_materials_catalog.json"),
            "JSON Files (*.json)",
        )
        if not target:
            return

        try:
            out_path = export_catalog_to_json_file(self.repo, target)
            QMessageBox.information(
                self,
                "Catalog Exported",
                f"Raw materials catalog successfully exported to:\n{out_path}",
            )
        except Exception as err:
            QMessageBox.critical(self, "Export Error", f"Failed to export catalog:\n{err}")

    def _export_selected_recipes(self) -> None:
        selected_recipes: list[Recipe] = []
        for i in range(self.recipe_list.count()):
            item = self.recipe_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                r = item.data(Qt.ItemDataRole.UserRole)
                if isinstance(r, Recipe):
                    selected_recipes.append(r)

        if not selected_recipes:
            return

        if len(selected_recipes) == 1:
            rec = selected_recipes[0]
            clean_name = re.sub(r"[\/*?:\"<>| ]", "_", rec.name)
            target, _ = QFileDialog.getSaveFileName(
                self,
                "Export Formula JSON",
                str(Path(self._get_start_dir()) / f"{clean_name}_v{rec.version}.json"),
                "JSON Files (*.json)",
            )
            if not target:
                return
            try:
                save_recipe_to_file(rec, target)
                QMessageBox.information(self, "Export Complete", f"Exported '{rec.name}' to:\n{target}")
            except Exception as err:
                QMessageBox.critical(self, "Export Error", f"Failed to export recipe:\n{err}")
        else:
            dest_dir = QFileDialog.getExistingDirectory(
                self,
                "Select Destination Directory for Formulas",
                self._get_start_dir(),
            )
            if not dest_dir:
                return

            saved_count = 0
            dest_path = Path(dest_dir)
            try:
                for rec in selected_recipes:
                    clean_name = re.sub(r"[\/*?:\"<>| ]", "_", rec.name)
                    file_name = f"{clean_name}_v{rec.version}.json"
                    save_recipe_to_file(rec, dest_path / file_name)
                    saved_count += 1

                QMessageBox.information(
                    self,
                    "Export Complete",
                    f"Successfully exported {saved_count} formulas to:\n{dest_dir}",
                )
            except Exception as err:
                QMessageBox.critical(self, "Export Error", f"Error during export:\n{err}")

    def _restore_workspace(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Workspace Backup File",
            self._get_start_dir(),
            "JSON Files (*.json)",
        )
        if not file_path:
            return

        try:
            stats = inspect_workspace_file(file_path)
        except Exception as err:
            QMessageBox.critical(self, "Invalid Backup File", f"Could not inspect workspace file:\n{err}")
            return

        draft_note = ""
        draft_total = len(stats.get("draft_recipes", ())) + len(stats.get("draft_revisions", ()))
        if draft_total > 0:
            draft_note = f"• In-Progress Drafts (sum ≠ 100%): {draft_total}<br>"

        prompt = (
            f"<b>Workspace Backup Inspection:</b><br>"
            f"• Exported At: {stats['exported_at']}<br>"
            f"• Formulas: {stats['recipe_count']} active (+{stats['archived_recipe_count']} archived)<br>"
            f"• Master Raw Materials: {stats['catalog_count']}<br>"
            f"• Historical Revisions: {stats['revision_count']}<br>"
            f"• Production Batches: {stats['batch_count']}<br>"
            f"{draft_note}<br>"
            f"<b style='color: #dc3545;'>Warning:</b> Restoring will replace the active database.<br>"
            f"A pre-restore safety snapshot (.bak) will automatically be saved.<br><br>"
            f"Proceed with restoring workspace?"
        )

        reply = QMessageBox.question(
            self,
            "Confirm Workspace Restoration",
            prompt,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            safety_backup = restore_workspace_from_json(self.repo, file_path)
            note = f"\n\nA safety backup was preserved at:\n{safety_backup}" if safety_backup else ""
            QMessageBox.information(
                self,
                "Restoration Successful",
                f"Workspace was restored successfully!{note}",
            )
            self._populate_recipes()
            self.workspace_restored.emit()
        except Exception as err:
            QMessageBox.critical(self, "Restoration Failed", f"Failed to restore workspace:\n{err}")

    def _import_recipes(self) -> None:
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Select Formula JSON File(s) to Import",
            self._get_start_dir(),
            "JSON Files (*.json)",
        )
        if not file_paths:
            return

        imported_count = 0
        for f in file_paths:
            try:
                recipe = load_recipe_from_file(f)
                existing = self.repo.get_recipe(recipe.id, include_archived=True)
                collision_mode = "copy"

                if existing is not None:
                    reply = QMessageBox.question(
                        self,
                        "Formula Collision",
                        f"Formula '{recipe.name}' (id: {recipe.id}) already exists.<br><br>"
                        f"Choose 'Yes' to import as a new copy, or 'No' to overwrite.",
                        QMessageBox.StandardButton.Yes
                        | QMessageBox.StandardButton.No
                        | QMessageBox.StandardButton.Cancel,
                        QMessageBox.StandardButton.Yes,
                    )
                    if reply == QMessageBox.StandardButton.Cancel:
                        continue
                    collision_mode = "copy" if reply == QMessageBox.StandardButton.Yes else "overwrite"

                self.service.import_recipe(recipe, collision_mode=collision_mode)
                imported_count += 1
            except Exception as err:
                QMessageBox.critical(self, "Import Error", f"Failed to import '{f}':\n{err}")

        if imported_count > 0:
            QMessageBox.information(
                self,
                "Import Complete",
                f"Successfully imported {imported_count} formula(s).",
            )
            self._populate_recipes()
            self.recipes_imported.emit()

    def _import_catalog(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Catalog JSON File",
            self._get_start_dir(),
            "JSON Files (*.json)",
        )
        if not file_path:
            return

        try:
            count = import_catalog_from_json_file(self.repo, file_path)
            QMessageBox.information(
                self,
                "Catalog Imported",
                f"Successfully imported and updated {count} raw material(s) in the master catalog.",
            )
            self.catalog_imported.emit()
        except Exception as err:
            QMessageBox.critical(self, "Catalog Import Error", f"Failed to import catalog:\n{err}")

    def _reset_workspace(self) -> None:
        recipes = self.repo.list_all_recipes_data()
        catalog = self.repo.list_catalog_ingredients()
        revisions = self.repo.list_all_revisions()
        batches = self.repo.list_batches()

        prompt = (
            "<b>Confirm Workspace Reset</b><br><br>"
            "This will clear your local formulation environment back to an empty slate:<br>"
            f"• Formulas: {len(recipes)}<br>"
            f"• Master Raw Materials: {len(catalog)}<br>"
            f"• Historical Revisions: {len(revisions)}<br>"
            f"• Production Batches: {len(batches)}<br><br>"
            "<b style='color: #dc3545;'>Warning:</b> "
            "An automated safety backup (.bak) will be preserved before resetting.<br><br>"
            "Proceed with resetting workspace to default?"
        )

        reply = QMessageBox.question(
            self,
            "Reset Workspace to Clean Slate",
            prompt,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        confirm_text, ok = QInputDialog.getText(
            self,
            "Type RESET to Confirm",
            "This operation cannot be undone. Type <b>RESET</b> below to proceed:",
        )
        if not ok or confirm_text.strip().upper() != "RESET":
            QMessageBox.information(
                self,
                "Reset Cancelled",
                "Confirmation keyword did not match 'RESET'. Workspace reset was aborted.",
            )
            return

        try:
            backup_path = self.repo.reset_workspace_data()
            note = f"\n\nA safety backup was preserved at:\n{backup_path}" if backup_path else ""
            QMessageBox.information(
                self,
                "Workspace Reset Complete",
                f"Workspace has been reset to a clean empty slate.{note}",
            )
            self._populate_recipes()
            self.workspace_restored.emit()
        except Exception as err:
            QMessageBox.critical(self, "Reset Error", f"Failed to reset workspace:\n{err}")
