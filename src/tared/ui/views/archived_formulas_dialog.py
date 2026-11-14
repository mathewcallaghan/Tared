"""Dialog for viewing, inspecting history, and restoring archived formulas."""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from tared.storage.base import RecipeRepository
from tared.ui.views.batch_history_dialog import BatchHistoryDialog


class ArchivedFormulasDialog(QDialog):
    """Modal dialog displaying archived formulas with restore and historical inspection actions."""

    recipe_restored = pyqtSignal(str)
    formula_duplicated = pyqtSignal(str)

    def __init__(self, repository: RecipeRepository, parent: Any = None) -> None:
        super().__init__(parent)
        self.repo = repository
        self.setWindowTitle("Archived Formulas")
        self.resize(860, 460)

        self._archived: list[dict[str, Any]] = []
        self._setup_ui()
        self._load_archived()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        info_label = QLabel(
            "Archived formulas are hidden from the main library while retaining all revision snapshots "
            "and production batch logs. Restore a formula to edit it, or view its history directly."
        )
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: #475569; font-size: 9pt; margin-bottom: 4px;")
        layout.addWidget(info_label)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter archived formulas by name or description...")
        self.search_input.setClearButtonEnabled(True)
        layout.addWidget(self.search_input)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            [
                "Formula Name",
                "Rev",
                "Last Updated",
                "Ingredients",
                "Batches",
                "Description / SOP",
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
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)

        btn_layout = QHBoxLayout()
        self.btn_restore = QPushButton("Restore to Active Library")
        self.btn_restore.setStyleSheet("font-weight: bold; padding: 6px 14px;")
        self.btn_restore.setEnabled(False)

        self.btn_history = QPushButton("View History...")
        self.btn_history.setStyleSheet("padding: 6px 14px;")
        self.btn_history.setEnabled(False)

        self.btn_close = QPushButton("Close")
        self.btn_close.setStyleSheet("padding: 6px 14px;")

        btn_layout.addWidget(self.btn_history)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_restore)
        btn_layout.addWidget(self.btn_close)
        layout.addLayout(btn_layout)

        self.search_input.textChanged.connect(self._filter_rows)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.itemDoubleClicked.connect(lambda: self._open_selected_history())
        self.btn_history.clicked.connect(self._open_selected_history)
        self.btn_restore.clicked.connect(self._restore_selected)
        self.btn_close.clicked.connect(self.accept)

    def _load_archived(self) -> None:
        self._archived = self.repo.list_archived_recipes()
        self.table.setRowCount(len(self._archived))

        for row, rec in enumerate(self._archived):
            item_name = QTableWidgetItem(str(rec.get("name", "")))
            item_name.setData(Qt.ItemDataRole.UserRole, rec.get("id", ""))

            item_ver = QTableWidgetItem(f"v{rec.get('version', 1)}")
            item_ver.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            item_updated = QTableWidgetItem(str(rec.get("updated_at", "") or "—"))

            item_ing_count = QTableWidgetItem(str(rec.get("ingredient_count", 0)))
            item_ing_count.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            item_batch_count = QTableWidgetItem(str(rec.get("batch_count", 0)))
            item_batch_count.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            item_desc = QTableWidgetItem(str(rec.get("description", "")))

            self.table.setItem(row, 0, item_name)
            self.table.setItem(row, 1, item_ver)
            self.table.setItem(row, 2, item_updated)
            self.table.setItem(row, 3, item_ing_count)
            self.table.setItem(row, 4, item_batch_count)
            self.table.setItem(row, 5, item_desc)

        if self.search_input.text().strip():
            self._filter_rows(self.search_input.text())

        if self._archived:
            self.table.selectRow(0)
        else:
            self._on_selection_changed()

    def _filter_rows(self, text: str) -> None:
        query = text.strip().lower()
        for row in range(self.table.rowCount()):
            name = (self.table.item(row, 0).text() if self.table.item(row, 0) else "").lower()
            desc = (self.table.item(row, 5).text() if self.table.item(row, 5) else "").lower()
            self.table.setRowHidden(row, query not in name and query not in desc)

    def _get_selected_record(self) -> dict[str, Any] | None:
        selected = self.table.selectionModel().selectedRows()
        if not selected:
            return None
        row = selected[0].row()
        if 0 <= row < len(self._archived):
            return self._archived[row]
        return None

    def _on_selection_changed(self) -> None:
        has_sel = self._get_selected_record() is not None
        self.btn_restore.setEnabled(has_sel)
        self.btn_history.setEnabled(has_sel)

    def _open_selected_history(self) -> None:
        rec = self._get_selected_record()
        if rec is None:
            return
        dialog = BatchHistoryDialog(
            recipe_id=str(rec["id"]),
            recipe_name=str(rec.get("name", "Archived Formula")),
            repository=self.repo,
            parent=self,
            allow_restore=False,
        )
        dialog.formula_duplicated.connect(self.formula_duplicated.emit)
        dialog.exec()

    def _restore_selected(self) -> None:
        rec = self._get_selected_record()
        if rec is None:
            return

        recipe_id = str(rec["id"])
        recipe_name = str(rec.get("name", ""))
        version = int(rec.get("version", 1))

        reply = QMessageBox.question(
            self,
            "Restore Archived Formula",
            f"Restore '{recipe_name}' (v{version}) to the active formula library?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        if self.repo.restore_recipe(recipe_id):
            self._load_archived()
            self.recipe_restored.emit(recipe_id)
