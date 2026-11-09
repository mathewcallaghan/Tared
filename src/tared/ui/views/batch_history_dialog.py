"""Dialog displaying historical batch run records with lossless PDF reprinting capability."""

import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from tared.core.models import BatchItem, BatchResult
from tared.printing.renderer import export_batch_sheet_to_pdf
from tared.storage.base import RecipeRepository


class BatchHistoryDialog(QDialog):
    """Modal dialog displaying logged production batches and allowing re-exporting of batch sheets."""

    def __init__(
        self,
        recipe_id: str,
        recipe_name: str,
        repository: RecipeRepository,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.recipe_id = recipe_id
        self.recipe_name = recipe_name
        self.repo = repository
        self.setWindowTitle(f"Production Batch History - {recipe_name}")
        self.resize(880, 480)

        self._batches: list[dict[str, Any]] = []
        self._setup_ui()
        self._load_batches()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

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

        layout.addWidget(self.table)

        btn_layout = QHBoxLayout()
        self.btn_reprint = QPushButton("Reprint Selected Batch...")
        self.btn_reprint.setStyleSheet("font-weight: bold; padding: 6px 14px;")
        self.btn_reprint.setEnabled(False)

        self.btn_close = QPushButton("Close")
        self.btn_close.setStyleSheet("padding: 6px 14px;")

        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_reprint)
        btn_layout.addWidget(self.btn_close)
        layout.addLayout(btn_layout)

        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.itemDoubleClicked.connect(lambda: self._reprint_selected_batch())
        self.btn_reprint.clicked.connect(self._reprint_selected_batch)
        self.btn_close.clicked.connect(self.accept)

    def _load_batches(self) -> None:
        self._batches = self.repo.list_batches(recipe_id=self.recipe_id)
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

    def _on_selection_changed(self) -> None:
        self.btn_reprint.setEnabled(len(self.table.selectedItems()) > 0)

    def _reprint_selected_batch(self) -> None:
        selected_indexes = self.table.selectionModel().selectedRows()
        if not selected_indexes:
            return

        row = selected_indexes[0].row()
        batch_record = self._batches[row]
        batch_num = batch_record.get("batch_number", "UNASSIGNED")
        recipe_ver = int(batch_record.get("recipe_version", 1))

        # 1. Recover historical formulation metadata from revision snapshot
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

            # 2. Reconstruct items and calculate exact discrete allocation deviation
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

            # 3. Transparent reprint audit timestamp
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
