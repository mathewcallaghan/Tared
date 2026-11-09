"""Dialog for picking multiple ingredients from the master catalog to insert into a recipe."""

from typing import Any

from PyQt6.QtCore import Qt
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
        self.table.verticalHeader().setVisible(False)

        header = self.table.horizontalHeader()
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

            cost_item = QTableWidgetItem(f"${item.cost_per_unit:.2f}/kg")
            cost_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            supp_item = QTableWidgetItem(item.supplier)

            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, cost_item)
            self.table.setItem(row, 2, supp_item)

    def _filter(self, text: str) -> None:
        query = text.lower().strip()
        for row in range(self.table.rowCount()):
            name_cell = self.table.item(row, 0)
            supp_cell = self.table.item(row, 2)
            name = name_cell.text().lower() if name_cell else ""
            supp = supp_cell.text().lower() if supp_cell else ""
            self.table.setRowHidden(row, query not in name and query not in supp)

    def _on_selection_changed(self) -> None:
        self.btn_insert.setEnabled(len(self.table.selectionModel().selectedRows()) > 0)

    def accept(self) -> None:
        self.selected_items = []
        for idx in self.table.selectionModel().selectedRows():
            item_data = self.table.item(idx.row(), 0).data(Qt.ItemDataRole.UserRole)
            if item_data is not None:
                self.selected_items.append(item_data)
        super().accept()
