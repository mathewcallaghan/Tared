"""Dialog for picking multiple ingredients from the master catalog to insert into a recipe."""

from typing import Any

from PyQt6.QtCore import QItemSelectionModel, Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from tared.core.formatting import format_currency
from tared.core.models import CatalogIngredient
from tared.storage.base import RecipeRepository


class CatalogPickerDialog(QDialog):
    """Modal dialog allowing selection of one or more raw materials for insertion."""

    def __init__(self, repository: RecipeRepository, parent: Any = None) -> None:
        super().__init__(parent)
        self.repo = repository
        self.selected_items: list[CatalogIngredient] = []
        self.setWindowTitle("Select Ingredients from Catalog")
        self.resize(550, 450)

        self._catalog = self.repo.list_catalog_ingredients()
        self._setup_ui()
        self._populate()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search by name or supplier...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._filter)
        layout.addWidget(self.search_input)

        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Raw Material", "Default Cost", "Supplier"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        v_header = self.table.verticalHeader()
        if v_header is not None:
            v_header.setVisible(False)

        header = self.table.horizontalHeader()
        assert header is not None
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table)

        btn_layout = QHBoxLayout()
        self.btn_insert = QPushButton("Insert Selected")
        self.btn_insert.setStyleSheet("font-weight: bold;")
        self.btn_insert.setEnabled(False)
        self.btn_cancel = QPushButton("Cancel")

        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_insert)
        btn_layout.addWidget(self.btn_cancel)
        layout.addLayout(btn_layout)

        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.btn_insert.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.reject)

    def _populate(self) -> None:
        self.table.setRowCount(len(self._catalog))
        for row, item in enumerate(self._catalog):
            name_item = QTableWidgetItem(item.name)
            name_item.setData(Qt.ItemDataRole.UserRole, item)

            cost_item = QTableWidgetItem(f"{format_currency(item.cost_per_unit)}/kg")
            cost_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            supp_item = QTableWidgetItem(item.supplier)

            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, cost_item)
            self.table.setItem(row, 2, supp_item)

    def _filter(self, text: str) -> None:
        query = text.lower().strip()
        sel_model = self.table.selectionModel()
        tbl_model = self.table.model()
        for row in range(self.table.rowCount()):
            name_cell = self.table.item(row, 0)
            supp_cell = self.table.item(row, 2)
            name = name_cell.text().lower() if name_cell else ""
            supp = supp_cell.text().lower() if supp_cell else ""
            hidden = query not in name and query not in supp
            self.table.setRowHidden(row, hidden)
            if (
                hidden
                and sel_model is not None
                and tbl_model is not None
                and sel_model.isRowSelected(row, self.table.rootIndex())
            ):
                sel_model.select(
                    tbl_model.index(row, 0),
                    QItemSelectionModel.SelectionFlag.Deselect | QItemSelectionModel.SelectionFlag.Rows,
                )
        self._on_selection_changed()

    def _on_selection_changed(self) -> None:
        sel_model = self.table.selectionModel()
        if sel_model is None:
            self.btn_insert.setEnabled(False)
            return
        visible_selected = [idx for idx in sel_model.selectedRows() if not self.table.isRowHidden(idx.row())]
        self.btn_insert.setEnabled(len(visible_selected) > 0)

    def accept(self) -> None:
        self.selected_items = []
        sel_model = self.table.selectionModel()
        if sel_model is not None:
            for idx in sel_model.selectedRows():
                if self.table.isRowHidden(idx.row()):
                    continue
                cell = self.table.item(idx.row(), 0)
                item_data = cell.data(Qt.ItemDataRole.UserRole) if cell is not None else None
                if item_data is not None:
                    self.selected_items.append(item_data)
        super().accept()
