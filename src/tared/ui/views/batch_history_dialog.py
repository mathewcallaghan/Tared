"""Dialog displaying production batch runs and historical formula revisions (v1..vN)."""

from __future__ import annotations

import decimal
import html
import json
import os
import re
import uuid
from collections.abc import Callable
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

from tared.config import ConfigManager
from tared.core.calculator import (
    CALCULATION_CONTEXT,
    PercentageSumError,
    RecipeCalculationError,
    validate_recipe_percentages,
)
from tared.core.formatting import format_currency, format_percentage
from tared.core.models import BatchItem, BatchResult, Ingredient, Recipe
from tared.core.service import RecipeService, summarize_revision_diff
from tared.printing.renderer import export_batch_sheet_to_pdf
from tared.storage.base import RecipeRepository, StorageError


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
        config: ConfigManager | None = None,
        confirm_restore_cb: Callable[[Recipe], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self.recipe_id = recipe_id
        self.recipe_name = recipe_name
        self.repo = repository
        self.allow_restore = allow_restore
        self.config = config
        self.confirm_restore_cb = confirm_restore_cb
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
        assert header is not None
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
        assert rev_header is not None
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
        self.rev_detail_label.setStyleSheet("font-size: 9pt; padding: 4px 2px;")
        detail_layout.addWidget(self.rev_detail_label)

        self.rev_ingredients_table = QTableWidget()
        self.rev_ingredients_table.setColumnCount(3)
        self.rev_ingredients_table.setHorizontalHeaderLabels(["Ingredient", "Formula %", "Unit Cost ($/kg)"])
        self.rev_ingredients_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.rev_ingredients_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        ing_header = self.rev_ingredients_table.horizontalHeader()
        assert ing_header is not None
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
            item_num = QTableWidgetItem(str(b.get("batch_number") or ""))
            item_date = QTableWidgetItem(str(b.get("created_at") or ""))
            raw_ver = b.get("recipe_version")
            ver_num = 1 if raw_ver is None or isinstance(raw_ver, bool) else int(raw_ver)
            item_ver = QTableWidgetItem(f"v{ver_num}")
            item_ver.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_op = QTableWidgetItem(str(b.get("operator") or ""))
            raw_tw = b.get("target_weight")
            item_weight = QTableWidgetItem(f"{raw_tw if raw_tw is not None else '0'} g")
            item_weight.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            raw_cost = b.get("total_cost")
            try:
                formatted_cost = format_currency(Decimal(str(raw_cost if raw_cost is not None else "0.00")))
            except (InvalidOperation, ValueError, TypeError):
                formatted_cost = f"${raw_cost if raw_cost is not None else '0.00'}"
            item_cost = QTableWidgetItem(formatted_cost)
            item_cost.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            item_notes = QTableWidgetItem(str(b.get("notes") or ""))

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
        sel_model = self.revisions_table.selectionModel()
        selected = sel_model.selectedRows() if sel_model is not None else []
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
            raw_pct = it.get("percentage")
            try:
                pct_str = f"{format_percentage(Decimal(str(raw_pct if raw_pct is not None else '0')))}%"
            except (InvalidOperation, ValueError):
                pct_str = str(raw_pct if raw_pct is not None else "0")
            raw_cost = it.get("cost_per_unit")
            try:
                cost_str = format_currency(Decimal(str(raw_cost if raw_cost is not None else "0.00")))
            except (InvalidOperation, ValueError):
                cost_str = str(raw_cost if raw_cost is not None else "0.00")

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
                cost_per_unit=Decimal(
                    str(it.get("cost_per_unit") if it.get("cost_per_unit") is not None else "0.00")
                ),
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
        sel_model = self.revisions_table.selectionModel()
        selected = sel_model.selectedRows() if sel_model is not None else []
        if not selected:
            return

        rev = self._revisions[selected[0].row()]
        try:
            rev_recipe = self._build_recipe_from_revision(rev)
        except (json.JSONDecodeError, KeyError, InvalidOperation, ValueError, TypeError) as err:
            QMessageBox.critical(self, "Revision Error", f"Could not load revision snapshot:\n{err}")
            return

        if self.confirm_restore_cb is not None and not self.confirm_restore_cb(rev_recipe):
            return

        self.revision_restored.emit(rev_recipe)
        self.accept()

    def _duplicate_selected_revision(self) -> None:
        sel_model = self.revisions_table.selectionModel()
        selected = sel_model.selectedRows() if sel_model is not None else []
        if not selected:
            return

        rev = self._revisions[selected[0].row()]
        try:
            rev_recipe = self._build_recipe_from_revision(rev)
            is_incomplete_draft = False
            try:
                validate_recipe_percentages(rev_recipe)
            except PercentageSumError:
                is_incomplete_draft = True

            new_recipe = Recipe(
                id=str(uuid.uuid4()),
                name=f"{rev_recipe.name} (v{rev_recipe.version} Copy)",
                description=rev_recipe.description,
                version=1,
                is_draft=is_incomplete_draft,
                ingredients=rev_recipe.ingredients,
            )
            saved = RecipeService(self.repo).save_recipe(new_recipe)
        except (
            json.JSONDecodeError,
            KeyError,
            InvalidOperation,
            ValueError,
            TypeError,
            RecipeCalculationError,
            StorageError,
        ) as err:
            QMessageBox.critical(self, "Duplicate Error", f"Could not duplicate revision:\n{err}")
            return

        self.formula_duplicated.emit(saved.id)
        if saved.is_draft:
            msg = (
                f"Revision v{rev_recipe.version} does not sum to 100.00% and was duplicated as an "
                f"in-progress Draft:\n'{saved.name}' (v1 Draft)."
            )
        else:
            msg = f"Revision v{rev_recipe.version} was duplicated into the active library as:\n'{saved.name}' (v1)."
        QMessageBox.information(self, "Formula Duplicated", msg)

    def _reprint_selected_batch(self) -> None:
        sel_model = self.table.selectionModel()
        selected_indexes = sel_model.selectedRows() if sel_model is not None else []
        if not selected_indexes:
            return

        row = selected_indexes[0].row()
        batch_record = self._batches[row]
        batch_num = str(batch_record.get("batch_number") or "UNASSIGNED")
        recipe_ver = int(batch_record.get("recipe_version") or 1)

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
            try:
                recipe_record = self.repo.get_recipe(self.recipe_id, include_archived=True)
            except (ValueError, InvalidOperation):
                recipe_record = None
            sheet_recipe_name = recipe_record.name if recipe_record else self.recipe_name
            sheet_recipe_desc = recipe_record.description if recipe_record else ""

        clean_name = re.sub(r'[\/*?:"<>| ]', "_", sheet_recipe_name)
        clean_batch = re.sub(r'[\/*?:"<>| ]', "_", batch_num)
        suggested_name = f"{clean_name}_{clean_batch}_reprint.pdf"

        start_dir = self.config.get_resolved_export_dir() if self.config else Path.home()
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Reprint Batch Sheet PDF",
            str(start_dir / suggested_name),
            "PDF Files (*.pdf)",
        )
        if not file_path:
            return
        if not file_path.lower().endswith(".pdf"):
            file_path = f"{file_path}.pdf"

        dest_path = Path(file_path).resolve()
        staging_pdf = dest_path.parent / f".tmp_{dest_path.name}_{uuid.uuid4().hex[:8]}"
        try:
            with decimal.localcontext(CALCULATION_CONTEXT):
                raw_target_w = batch_record.get("target_weight")
                target_w = Decimal(str(raw_target_w if raw_target_w is not None else "0"))
                precision_val = Decimal(str(batch_record.get("precision") or "0.01"))
                items_raw = json.loads(batch_record.get("items_json") or "[]")
                items: list[BatchItem] = []
                max_dev = Decimal("0")
                high_dev = []
                zero_wt = []

                total_item_pct = sum(
                    (
                        Decimal(str(it.get("percentage") if it.get("percentage") is not None else "0"))
                        for it in items_raw
                    ),
                    Decimal("0"),
                )
                norm_pct = total_item_pct if total_item_pct > Decimal("0") else Decimal("100.00")

                for it in items_raw:
                    raw_pct = it.get("percentage")
                    pct = Decimal(str(raw_pct if raw_pct is not None else "0"))
                    raw_scaled = it.get("scaled_weight")
                    scaled_w = Decimal(str(raw_scaled if raw_scaled is not None else "0"))
                    ing_name = str(it.get("ingredient_name") or "")

                    if it.get("raw_weight") is not None:
                        raw_w = Decimal(str(it["raw_weight"]))
                    elif raw_pct is not None and pct > Decimal("0") and target_w > Decimal("0"):
                        raw_w = (target_w * pct) / norm_pct
                    else:
                        raw_w = scaled_w

                    deviation = abs(scaled_w - raw_w)
                    if deviation > max_dev:
                        max_dev = deviation

                    if raw_w > Decimal("0"):
                        if (deviation / raw_w) > Decimal("0.25") and pct > Decimal("0"):
                            high_dev.append(ing_name)

                    if scaled_w == Decimal("0") and pct > Decimal("0"):
                        zero_wt.append(ing_name)

                    raw_tare = it.get("cumulative_tare")
                    raw_cost = it.get("cost")
                    raw_cpu = it.get("cost_per_unit")
                    items.append(
                        BatchItem(
                            ingredient_name=ing_name,
                            percentage=pct,
                            raw_weight=raw_w,
                            scaled_weight=scaled_w,
                            cumulative_tare=Decimal(str(raw_tare if raw_tare is not None else "0")),
                            cost=Decimal(str(raw_cost if raw_cost is not None else "0")),
                            cost_per_unit=Decimal(str(raw_cpu if raw_cpu is not None else "0.00")),
                        )
                    )

                raw_total_w = batch_record.get("total_weight")
                raw_total_c = batch_record.get("total_cost")
                reconstructed_batch = BatchResult(
                    recipe_name=sheet_recipe_name,
                    target_weight=target_w,
                    precision=precision_val,
                    items=tuple(items),
                    total_weight=Decimal(str(raw_total_w if raw_total_w is not None else "0")),
                    total_cost=Decimal(str(raw_total_c if raw_total_c is not None else "0")),
                    recipe_version=recipe_ver,
                    recipe_id=self.recipe_id,
                    recipe_description=sheet_recipe_desc,
                    zero_weight_ingredients=tuple(zero_wt),
                    high_deviation_ingredients=tuple(high_dev),
                    max_allocation_deviation=max_dev,
                )

            reprint_local_time = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
            created_at_str = str(batch_record.get("created_at") or "").strip()
            timestamp_note = (
                f"{created_at_str} [Reprinted: {reprint_local_time}]"
                if created_at_str
                else f"Reprinted: {reprint_local_time}"
            )
            raw_cd = batch_record.get("cost_decimals")
            reprint_cost_decimals: int | None = None
            if raw_cd is not None and not isinstance(raw_cd, bool):
                try:
                    parsed_cd = int(raw_cd)
                    if 0 <= parsed_cd <= 10:
                        reprint_cost_decimals = parsed_cd
                except (ValueError, TypeError):
                    reprint_cost_decimals = None

            export_batch_sheet_to_pdf(
                batch=reconstructed_batch,
                output_path=staging_pdf,
                batch_number=batch_num,
                operator=str(batch_record.get("operator") or "Unassigned"),
                notes=str(batch_record.get("notes") or ""),
                timestamp_str=timestamp_note,
                cost_decimals=reprint_cost_decimals,
            )
            os.replace(str(staging_pdf), str(dest_path))

            QMessageBox.information(
                self,
                "Reprint Complete",
                f"Batch sheet reprint exported successfully to:\n{dest_path}",
            )
        except Exception as err:
            QMessageBox.critical(self, "Reprint Error", f"Failed to export reprint PDF:\n{err}")
        finally:
            if staging_pdf.exists():
                try:
                    staging_pdf.unlink()
                except OSError:
                    pass
