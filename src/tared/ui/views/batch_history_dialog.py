"""Dialog displaying production batch runs and historical formula revisions (v1..vN)."""

from __future__ import annotations

import html
import json
import re
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from tared.core.formatting import format_currency, format_percentage
from tared.core.models import BatchItem, BatchResult, Ingredient, Recipe
from tared.core.service import RecipeService, summarize_revision_diff
from tared.printing.renderer import export_batch_sheet_to_pdf
from tared.storage.base import RecipeRepository


class BatchHistoryDialog(QDialog):
    """Two-tab history dialog for Production Batches (reprintable) and Formula Revisions."""

    revision_restored = pyqtSignal(object)
    formula_duplicated = pyqtSignal(str)

    def __init__(
        self,
        recipe_id: str,
        recipe_name: str,
        repository: RecipeRepository,
        parent: Any = None,
        allow_restore: bool = True,
    ) -> None:
        super().__init__(parent)
        self.recipe_id = recipe_id
        self.recipe_name = recipe_name
        self.repo = repository
        self.allow_restore = allow_restore
        self.setWindowTitle(f"Formula & Batch History - {recipe_name}")
        self.resize(940, 580)

        self._batches: list[dict[str, Any]] = []
        self._revisions: list[dict[str, Any]] = []
        self._setup_ui()
        self._load_batches()
        self._load_revisions()

        if not self._batches and self._revisions:
            self.tabs.setCurrentIndex(1)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        # Tab 1: Production Batches
        batches_tab = QWidget()
        batches_layout = QVBoxLayout(batches_tab)

        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            [
                "Batch #",
                "Recorded Date",
                "Rev",
                "Operator",
                "Target Mass",
                "Total Cost",
                "Notes",
            ]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)

        batches_layout.addWidget(self.table)

        batch_btn_layout = QHBoxLayout()
        self.btn_reprint = QPushButton("Reprint Selected Batch...")
        self.btn_reprint.setStyleSheet("font-weight: bold; padding: 6px 14px;")
        self.btn_reprint.setEnabled(False)
        batch_btn_layout.addStretch()
        batch_btn_layout.addWidget(self.btn_reprint)
        batches_layout.addLayout(batch_btn_layout)

        self.tabs.addTab(batches_tab, "Production Batches")

        # Tab 2: Formula Revisions
        revisions_tab = QWidget()
        revisions_layout = QVBoxLayout(revisions_tab)

        splitter = QSplitter(Qt.Orientation.Vertical)
        revisions_layout.addWidget(splitter)

        self.revisions_table = QTableWidget()
        self.revisions_table.setColumnCount(5)
        self.revisions_table.setHorizontalHeaderLabels(
            [
                "Rev",
                "Saved Date",
                "Formula Name",
                "Ingredients",
                "Changes vs. Previous",
            ]
        )
        self.revisions_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.revisions_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.revisions_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        rev_header = self.revisions_table.horizontalHeader()
        rev_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        rev_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        rev_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        rev_header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        rev_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        splitter.addWidget(self.revisions_table)

        detail_widget = QWidget()
        detail_layout = QVBoxLayout(detail_widget)
        detail_layout.setContentsMargins(0, 4, 0, 0)

        self.rev_detail_label = QLabel("Select a revision above to inspect its formulation snapshot.")
        self.rev_detail_label.setWordWrap(True)
        self.rev_detail_label.setStyleSheet("font-size: 9pt; color: #334155; padding: 4px 2px;")
        detail_layout.addWidget(self.rev_detail_label)

        self.rev_ingredients_table = QTableWidget()
        self.rev_ingredients_table.setColumnCount(3)
        self.rev_ingredients_table.setHorizontalHeaderLabels(["Ingredient", "Formula %", "Unit Cost ($/kg)"])
        self.rev_ingredients_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.rev_ingredients_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        ing_header = self.rev_ingredients_table.horizontalHeader()
        ing_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        ing_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        ing_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        detail_layout.addWidget(self.rev_ingredients_table)

        splitter.addWidget(detail_widget)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)

        rev_btn_layout = QHBoxLayout()
        self.btn_restore_revision = QPushButton("Restore Revision to Editor")
        self.btn_restore_revision.setStyleSheet("font-weight: bold; padding: 6px 14px;")
        self.btn_restore_revision.setEnabled(False)
        self.btn_restore_revision.setVisible(self.allow_restore)

        self.btn_duplicate_revision = QPushButton("Duplicate as New Formula")
        self.btn_duplicate_revision.setStyleSheet("padding: 6px 14px;")
        self.btn_duplicate_revision.setEnabled(False)

        rev_btn_layout.addStretch()
        rev_btn_layout.addWidget(self.btn_duplicate_revision)
        rev_btn_layout.addWidget(self.btn_restore_revision)
        revisions_layout.addLayout(rev_btn_layout)

        self.tabs.addTab(revisions_tab, "Formula Revisions")

        footer_layout = QHBoxLayout()
        self.btn_close = QPushButton("Close")
        self.btn_close.setStyleSheet("padding: 6px 14px;")
        footer_layout.addStretch()
        footer_layout.addWidget(self.btn_close)
        layout.addLayout(footer_layout)

        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.itemDoubleClicked.connect(lambda: self._reprint_selected_batch())
        self.btn_reprint.clicked.connect(self._reprint_selected_batch)

        self.revisions_table.itemSelectionChanged.connect(self._on_revision_selection_changed)
        self.btn_restore_revision.clicked.connect(self._restore_selected_revision)
        self.btn_duplicate_revision.clicked.connect(self._duplicate_selected_revision)
        self.btn_close.clicked.connect(self.accept)

    def _load_batches(self) -> None:
        self._batches = self.repo.list_batches(recipe_id=self.recipe_id)
        self.tabs.setTabText(0, f"Production Batches ({len(self._batches)})")
        self.table.setRowCount(len(self._batches))

        for row, b in enumerate(self._batches):
            item_num = QTableWidgetItem(b.get("batch_number", ""))
            item_date = QTableWidgetItem(b.get("created_at", ""))
            item_ver = QTableWidgetItem(f"v{b.get('recipe_version', 1)}")
            item_ver.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_op = QTableWidgetItem(b.get("operator", ""))
            item_weight = QTableWidgetItem(f"{b.get('target_weight', '0')} g")
            item_weight.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            item_cost = QTableWidgetItem(f"${b.get('total_cost', '0.00')}")
            item_cost.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            item_notes = QTableWidgetItem(b.get("notes", ""))

            self.table.setItem(row, 0, item_num)
            self.table.setItem(row, 1, item_date)
            self.table.setItem(row, 2, item_ver)
            self.table.setItem(row, 3, item_op)
            self.table.setItem(row, 4, item_weight)
            self.table.setItem(row, 5, item_cost)
            self.table.setItem(row, 6, item_notes)

        if self._batches:
            self.table.selectRow(0)

    def _load_revisions(self) -> None:
        self._revisions = self.repo.get_recipe_revisions(self.recipe_id)
        self.tabs.setTabText(1, f"Formula Revisions ({len(self._revisions)})")
        self.revisions_table.setRowCount(len(self._revisions))

        for row, rev in enumerate(self._revisions):
            prev_rev = self._revisions[row + 1] if row + 1 < len(self._revisions) else None
            diff_summary = summarize_revision_diff(prev_rev, rev)

            try:
                ing_items = json.loads(rev.get("ingredients_json", "[]"))
                ing_count = len(ing_items) if isinstance(ing_items, list) else 0
            except (json.JSONDecodeError, TypeError):
                ing_count = 0

            item_ver = QTableWidgetItem(f"v{rev.get('version', 1)}")
            item_ver.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_date = QTableWidgetItem(rev.get("created_at", "") or "—")
            item_name = QTableWidgetItem(rev.get("name", ""))
            item_count = QTableWidgetItem(str(ing_count))
            item_count.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_diff = QTableWidgetItem(diff_summary)

            self.revisions_table.setItem(row, 0, item_ver)
            self.revisions_table.setItem(row, 1, item_date)
            self.revisions_table.setItem(row, 2, item_name)
            self.revisions_table.setItem(row, 3, item_count)
            self.revisions_table.setItem(row, 4, item_diff)

        if self._revisions:
            self.revisions_table.selectRow(0)

    def _on_selection_changed(self) -> None:
        self.btn_reprint.setEnabled(len(self.table.selectedItems()) > 0)

    def _on_revision_selection_changed(self) -> None:
        selected = self.revisions_table.selectionModel().selectedRows()
        has_sel = len(selected) > 0
        self.btn_restore_revision.setEnabled(has_sel and self.allow_restore)
        self.btn_duplicate_revision.setEnabled(has_sel)

        if not has_sel:
            self.rev_detail_label.setText("Select a revision above to inspect its formulation snapshot.")
            self.rev_ingredients_table.setRowCount(0)
            return

        row = selected[0].row()
        rev = self._revisions[row]
        escaped_name = html.escape(str(rev.get("name", "")))
        raw_desc = (rev.get("description") or "").strip() or "No description / SOP recorded."
        escaped_desc = html.escape(raw_desc)
        self.rev_detail_label.setText(
            f"<b>Revision v{rev.get('version', 1)} — {escaped_name}</b><br>"
            f"<b>SOP / Description:</b> {escaped_desc}"
        )

        try:
            items = json.loads(rev.get("ingredients_json", "[]"))
            if not isinstance(items, list):
                items = []
        except (json.JSONDecodeError, TypeError):
            items = []

        self.rev_ingredients_table.setRowCount(len(items))
        for i_row, it in enumerate(items):
            name = str(it.get("name", ""))
            try:
                pct_str = f"{format_percentage(Decimal(str(it.get('percentage', '0'))))}%"
            except (InvalidOperation, ValueError):
                pct_str = str(it.get("percentage", "0"))
            try:
                cost_str = format_currency(Decimal(str(it.get("cost_per_unit", "0.00"))))
            except (InvalidOperation, ValueError):
                cost_str = str(it.get("cost_per_unit", "0.00"))

            name_item = QTableWidgetItem(name)
            pct_item = QTableWidgetItem(pct_str)
            pct_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            cost_item = QTableWidgetItem(cost_str)
            cost_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            self.rev_ingredients_table.setItem(i_row, 0, name_item)
            self.rev_ingredients_table.setItem(i_row, 1, pct_item)
            self.rev_ingredients_table.setItem(i_row, 2, cost_item)

    def _build_recipe_from_revision(self, rev: dict[str, Any]) -> Recipe:
        raw_items = json.loads(rev.get("ingredients_json", "[]"))
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError("Revision snapshot contains no ingredients.")

        ingredients = tuple(
            Ingredient(
                name=str(it["name"]),
                percentage=Decimal(str(it["percentage"])),
                cost_per_unit=Decimal(str(it.get("cost_per_unit", "0.00"))),
            )
            for it in raw_items
        )
        return Recipe(
            id=self.recipe_id,
            name=str(rev.get("name", self.recipe_name)),
            description=str(rev.get("description") or ""),
            version=int(rev.get("version", 1)),
            updated_at=str(rev.get("created_at") or ""),
            ingredients=ingredients,
        )

    def _restore_selected_revision(self) -> None:
        if not self.allow_restore:
            return
        selected = self.revisions_table.selectionModel().selectedRows()
        if not selected:
            return

        rev = self._revisions[selected[0].row()]
        try:
            rev_recipe = self._build_recipe_from_revision(rev)
        except (json.JSONDecodeError, KeyError, InvalidOperation, ValueError, TypeError) as err:
            QMessageBox.critical(self, "Revision Error", f"Could not load revision snapshot:\n{err}")
            return

        self.revision_restored.emit(rev_recipe)
        self.accept()

    def _duplicate_selected_revision(self) -> None:
        selected = self.revisions_table.selectionModel().selectedRows()
        if not selected:
            return

        rev = self._revisions[selected[0].row()]
        try:
            rev_recipe = self._build_recipe_from_revision(rev)
            new_recipe = Recipe(
                id=str(uuid.uuid4()),
                name=f"{rev_recipe.name} (v{rev_recipe.version} Copy)",
                description=rev_recipe.description,
                version=1,
                ingredients=rev_recipe.ingredients,
            )
            saved = RecipeService(self.repo).save_recipe(new_recipe)
        except (json.JSONDecodeError, KeyError, InvalidOperation, ValueError, TypeError) as err:
            QMessageBox.critical(self, "Duplicate Error", f"Could not duplicate revision:\n{err}")
            return

        self.formula_duplicated.emit(saved.id)
        QMessageBox.information(
            self,
            "Formula Duplicated",
            f"Revision v{rev_recipe.version} was duplicated into the active library as:\n'{saved.name}' (v1).",
        )

    def _reprint_selected_batch(self) -> None:
        selected_indexes = self.table.selectionModel().selectedRows()
        if not selected_indexes:
            return

        row = selected_indexes[0].row()
        batch_record = self._batches[row]
        batch_num = batch_record.get("batch_number", "UNASSIGNED")
        recipe_ver = int(batch_record.get("recipe_version", 1))

        rev_snapshot = None
        if hasattr(self.repo, "get_recipe_revision"):
            rev_snapshot = self.repo.get_recipe_revision(self.recipe_id, recipe_ver)
        if rev_snapshot is None:
            for r in self.repo.get_recipe_revisions(self.recipe_id):
                if r.get("version") == recipe_ver:
                    rev_snapshot = r
                    break

        if rev_snapshot:
            sheet_recipe_name = rev_snapshot.get("name") or self.recipe_name
            sheet_recipe_desc = rev_snapshot.get("description", "")
        else:
            recipe_record = self.repo.get_recipe(self.recipe_id, include_archived=True)
            sheet_recipe_name = recipe_record.name if recipe_record else self.recipe_name
            sheet_recipe_desc = recipe_record.description if recipe_record else ""

        clean_name = re.sub(r'[\/*?:"<>| ]', "_", sheet_recipe_name)
        clean_batch = re.sub(r'[\/*?:"<>| ]', "_", batch_num)
        suggested_name = f"{clean_name}_{clean_batch}_reprint.pdf"

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Reprint Batch Sheet PDF",
            str(Path.home() / suggested_name),
            "PDF Files (*.pdf)",
        )
        if not file_path:
            return

        try:
            target_w = Decimal(str(batch_record.get("target_weight", "0")))
            precision_val = Decimal(str(batch_record.get("precision", "0.01")))
            items_raw = json.loads(batch_record.get("items_json", "[]"))
            items: list[BatchItem] = []
            max_dev = Decimal("0")
            high_dev = []
            zero_wt = []

            for it in items_raw:
                pct = Decimal(str(it.get("percentage", "0")))
                scaled_w = Decimal(str(it.get("scaled_weight", "0")))
                ing_name = it.get("ingredient_name", "")

                if "raw_weight" in it:
                    raw_w = Decimal(str(it["raw_weight"]))
                else:
                    raw_w = (target_w * pct) / Decimal("100.00") if target_w > Decimal("0") else scaled_w

                deviation = abs(scaled_w - raw_w)
                if deviation > max_dev:
                    max_dev = deviation

                if raw_w > Decimal("0"):
                    if (deviation / raw_w) > Decimal("0.25") and pct > Decimal("0"):
                        high_dev.append(ing_name)

                if scaled_w == Decimal("0") and pct > Decimal("0"):
                    zero_wt.append(ing_name)

                items.append(
                    BatchItem(
                        ingredient_name=it.get("ingredient_name", ""),
                        percentage=pct,
                        raw_weight=raw_w,
                        scaled_weight=scaled_w,
                        cumulative_tare=Decimal(str(it.get("cumulative_tare", "0"))),
                        cost=Decimal(str(it.get("cost", "0"))),
                        cost_per_unit=Decimal(str(it.get("cost_per_unit", "0.00"))),
                    )
                )

            reconstructed_batch = BatchResult(
                recipe_name=sheet_recipe_name,
                target_weight=target_w,
                precision=precision_val,
                items=tuple(items),
                total_weight=Decimal(str(batch_record.get("total_weight", "0"))),
                total_cost=Decimal(str(batch_record.get("total_cost", "0"))),
                recipe_version=recipe_ver,
                recipe_id=self.recipe_id,
                recipe_description=sheet_recipe_desc,
                zero_weight_ingredients=tuple(zero_wt),
                high_deviation_ingredients=tuple(high_dev),
                max_allocation_deviation=max_dev,
            )

            reprint_local_time = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
            timestamp_note = f"{batch_record.get('created_at', '')} [Reprinted: {reprint_local_time}]"

            export_batch_sheet_to_pdf(
                batch=reconstructed_batch,
                output_path=file_path,
                batch_number=batch_num,
                operator=batch_record.get("operator", "Unassigned"),
                notes=batch_record.get("notes", ""),
                timestamp_str=timestamp_note,
            )

            QMessageBox.information(
                self,
                "Reprint Complete",
                f"Batch sheet reprint exported successfully to:\n{file_path}",
            )
        except Exception as err:
            QMessageBox.critical(self, "Reprint Error", f"Failed to export reprint PDF:\n{err}")
