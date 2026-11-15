"""Dialog for managing the master raw material catalog and shared price book."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from tared.core.formatting import MAX_UNIT_COST, format_currency
from tared.core.models import CatalogIngredient, Recipe
from tared.core.service import RecipeService
from tared.storage.base import RecipeRepository
from tared.ui.models.recipe_table_model import _format_decimal_for_edit, _parse_decimal_input


def _format_unit_cost(cost: Decimal) -> str:
    """Format unit cost preserving significant fractional decimals with a minimum of 2 decimals."""
    cents = cost * Decimal("100")
    if cents == cents.to_integral_value():
        return f"{cost:.2f}"
    s = f"{cost:f}"
    if "." in s:
        integer_part, frac_part = s.split(".", 1)
        frac_clean = frac_part.rstrip("0")
        if len(frac_clean) < 2:
            frac_clean = frac_clean.ljust(2, "0")
        return f"{integer_part}.{frac_clean}"
    return f"{s}.00"


class CatalogManagerDialog(QDialog):
    """Modal dialog allowing viewing, editing, searching, and syncing master raw material prices."""

    def __init__(
        self,
        repository: RecipeRepository,
        service: RecipeService,
        current_recipe: Recipe | None = None,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.repo = repository
        self.service = service
        self.current_recipe = current_recipe
        self.setWindowTitle("Master Raw Materials & Price Catalog")
        self.resize(920, 520)

        self._items: list[CatalogIngredient] = []
        self._is_loading = False
        self._setup_ui()
        self._load_catalog()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        top_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter raw materials by name, supplier, or SKU...")
        self.search_input.setClearButtonEnabled(True)
        top_layout.addWidget(self.search_input)
        layout.addLayout(top_layout)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            [
                "Raw Material Name",
                "Default Cost ($/kg)",
                "Supplier",
                "SKU / Code",
                "Notes",
                "Last Updated",
            ]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

        header = self.table.horizontalHeader()
        assert header is not None
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)

        layout.addWidget(self.table)

        btn_layout = QHBoxLayout()
        self.btn_add = QPushButton("+ Add Raw Material")
        self.btn_remove = QPushButton("- Remove Selected")
        self.btn_learn = QPushButton("Learn Prices from Active Formula")
        self.btn_close = QPushButton("Close")

        btn_layout.addWidget(self.btn_add)
        btn_layout.addWidget(self.btn_remove)
        btn_layout.addWidget(self.btn_learn)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_close)
        layout.addLayout(btn_layout)

        self.search_input.textChanged.connect(self._filter_items)
        self.table.cellChanged.connect(self._on_cell_changed)
        self.btn_add.clicked.connect(self._add_item)
        self.btn_remove.clicked.connect(self._remove_selected)
        self.btn_learn.clicked.connect(self._learn_from_recipe)
        self.btn_close.clicked.connect(self.accept)

        if not self.current_recipe or not self.current_recipe.ingredients:
            self.btn_learn.setEnabled(False)

    def _load_catalog(self) -> None:
        self._is_loading = True
        try:
            self._items = self.repo.list_catalog_ingredients()
            self.table.setRowCount(len(self._items))

            for row, item in enumerate(self._items):
                item_name = QTableWidgetItem(item.name)
                item_name.setData(Qt.ItemDataRole.UserRole, item.id)

                item_cost = QTableWidgetItem(_format_decimal_for_edit(_format_unit_cost(item.cost_per_unit)))
                item_cost.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

                item_supplier = QTableWidgetItem(item.supplier)
                item_sku = QTableWidgetItem(item.sku)
                item_notes = QTableWidgetItem(item.notes)

                item_date = QTableWidgetItem(item.updated_at)
                item_date.setFlags(item_date.flags() & ~Qt.ItemFlag.ItemIsEditable)

                self.table.setItem(row, 0, item_name)
                self.table.setItem(row, 1, item_cost)
                self.table.setItem(row, 2, item_supplier)
                self.table.setItem(row, 3, item_sku)
                self.table.setItem(row, 4, item_notes)
                self.table.setItem(row, 5, item_date)

            if self.search_input.text().strip():
                self._filter_items(self.search_input.text())
        finally:
            self._is_loading = False

    def _filter_items(self, text: str) -> None:
        query = text.strip().lower()
        for row in range(self.table.rowCount()):
            c0 = self.table.item(row, 0)
            c2 = self.table.item(row, 2)
            c3 = self.table.item(row, 3)
            name = (c0.text() if c0 is not None else "").lower()
            supplier = (c2.text() if c2 is not None else "").lower()
            sku = (c3.text() if c3 is not None else "").lower()
            matches = query in name or query in supplier or query in sku
            self.table.setRowHidden(row, not matches)

        sel_model = self.table.selectionModel()
        if sel_model is not None:
            selected = sel_model.selectedRows()
            if selected and any(self.table.isRowHidden(idx.row()) for idx in selected):
                self.table.clearSelection()

    def _on_cell_changed(self, row: int, col: int) -> None:
        if self._is_loading or row >= len(self._items):
            return

        item_id_item = self.table.item(row, 0)
        if not item_id_item:
            return
        item_id = item_id_item.data(Qt.ItemDataRole.UserRole)
        if not item_id:
            return

        name = item_id_item.text().strip()
        if not name:
            QMessageBox.warning(self, "Invalid Name", "Raw material name cannot be empty.")
            self._load_catalog()
            return

        c_cost = self.table.item(row, 1)
        cost_str = c_cost.text().strip().replace("$", "").strip() if c_cost is not None else "0.00"
        cost_was_empty = not cost_str
        try:
            cost = (
                Decimal("0.00")
                if cost_was_empty
                else _parse_decimal_input(cost_str, convert_kg=False, allow_mass_units=False)
            )
            if cost < Decimal("0"):
                raise ValueError("Cost cannot be negative")
            if cost > MAX_UNIT_COST:
                raise ValueError(f"Cost cannot exceed {MAX_UNIT_COST}")
        except (InvalidOperation, ValueError) as err:
            QMessageBox.warning(self, "Invalid Cost", f"Invalid cost value '{cost_str}': {err}")
            self._load_catalog()
            return

        c_supp = self.table.item(row, 2)
        c_sku = self.table.item(row, 3)
        c_notes = self.table.item(row, 4)
        supplier = c_supp.text().strip() if c_supp is not None else ""
        sku = c_sku.text().strip() if c_sku is not None else ""
        notes = c_notes.text().strip() if c_notes is not None else ""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        updated_item = CatalogIngredient(
            id=item_id,
            name=name,
            cost_per_unit=cost,
            supplier=supplier,
            sku=sku,
            notes=notes,
            updated_at=timestamp,
        )

        try:
            self.repo.update_catalog_ingredient(updated_item)
            self._items[row] = updated_item
            self._is_loading = True
            try:
                c0 = self.table.item(row, 0)
                if c0 is not None and c0.text() != name:
                    c0.setText(name)
                c1 = self.table.item(row, 1)
                if cost_was_empty and c1 is not None:
                    c1.setText(_format_decimal_for_edit(_format_unit_cost(cost)))
                c5 = self.table.item(row, 5)
                if c5 is not None:
                    c5.setText(timestamp)
            finally:
                self._is_loading = False
        except Exception as err:
            QMessageBox.critical(self, "Save Error", f"Could not update catalog item:\n{err}")
            self._load_catalog()

    def _add_item(self) -> None:
        existing_names = {item.name.strip().lower() for item in self._items}
        idx = len(self._items) + 1
        default_name = f"New Material {idx}"
        while default_name.lower() in existing_names:
            idx += 1
            default_name = f"New Material {idx}"
        new_item = CatalogIngredient(
            name=default_name,
            cost_per_unit=Decimal("0.00"),
            updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        try:
            self.repo.save_catalog_ingredient(new_item)
            self._load_catalog()
            for r in range(self.table.rowCount()):
                c0 = self.table.item(r, 0)
                if c0 is not None and c0.text() == default_name:
                    if self.table.isRowHidden(r):
                        self.search_input.clear()
                    self.table.selectRow(r)
                    self.table.editItem(c0)
                    break
        except Exception as err:
            QMessageBox.critical(self, "Add Error", f"Failed to add raw material:\n{err}")

    def _remove_selected(self) -> None:
        sel_model = self.table.selectionModel()
        if sel_model is None:
            return
        selected_rows = [idx for idx in sel_model.selectedRows() if not self.table.isRowHidden(idx.row())]
        if not selected_rows:
            return

        row = selected_rows[0].row()
        c0 = self.table.item(row, 0)
        if c0 is None:
            return
        item_name = c0.text()
        item_id = c0.data(Qt.ItemDataRole.UserRole)

        reply = QMessageBox.question(
            self,
            "Confirm Delete",
            f"Remove '{item_name}' from the master catalog?\n"
            "(Existing formulas using this ingredient will retain their saved costs)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.repo.delete_catalog_ingredient(item_id)
            self._load_catalog()

    def _learn_from_recipe(self) -> None:
        if not self.current_recipe:
            return

        diffs = self.service.preview_learn_from_recipe(self.current_recipe)
        if not diffs:
            QMessageBox.information(
                self,
                "No Price Updates",
                f"No new or updated ingredient prices (> $0.00) found in '{self.current_recipe.name}'.",
            )
            return

        diff_lines = []
        for name, old_cost, new_cost in diffs:
            if old_cost is None:
                diff_lines.append(f"• [New] {name}: {format_currency(new_cost)}/kg")
            else:
                diff_lines.append(f"• [Update] {name}: {format_currency(old_cost)} → {format_currency(new_cost)}/kg")

        prompt = (
            f"Found {len(diffs)} raw material price update(s) in '{self.current_recipe.name}':\n\n"
            + "\n".join(diff_lines[:15])
            + (f"\n...and {len(diff_lines) - 15} more." if len(diff_lines) > 15 else "")
            + "\n\nApply these updates to the master catalog?"
        )

        reply = QMessageBox.question(
            self,
            "Learn Prices from Formula",
            prompt,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Yes:
            count = self.service.update_catalog_from_recipe(self.current_recipe)
            self._load_catalog()
            QMessageBox.information(
                self,
                "Catalog Updated",
                f"Successfully updated master catalog with {count} ingredient price(s) "
                f"from '{self.current_recipe.name}'.",
            )
