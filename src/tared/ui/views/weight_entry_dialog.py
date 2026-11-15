"""Interactive dialog for batch formulation by discrete component weights."""

from __future__ import annotations

import decimal
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from PyQt6.QtCore import QModelIndex, Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QCompleter,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from tared.core.calculator import CALCULATION_CONTEXT
from tared.core.formatting import MAX_UNIT_COST, format_percentage
from tared.core.service import RecipeService
from tared.storage.base import RecipeRepository
from tared.ui.models.recipe_table_model import _format_decimal_for_edit, _parse_decimal_input
from tared.ui.views.catalog_picker_dialog import CatalogPickerDialog

MAX_TARGET_WEIGHT = Decimal("1000000000")


def _format_unit_cost(cost: Decimal) -> str:
    """Format unit cost preserving up to required micro-decimals without trailing truncation."""
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


def _format_weight_input(weight: Decimal, precision_decimals: int) -> str:
    """Format weight retaining full fractional decimals if finer than scale precision."""
    if weight <= Decimal("0"):
        return ""
    exp = weight.as_tuple().exponent
    weight_decimals = max(0, -exp) if isinstance(exp, int) else 0
    decimals = max(precision_decimals, weight_decimals)
    return f"{weight:.{decimals}f}"


class WeightIngredientCompleterDelegate(QStyledItemDelegate):
    """Provides inline raw material catalog autocompletion for ingredient names."""

    def __init__(self, get_names_cb: Any, parent: Any = None) -> None:
        super().__init__(parent)
        self.get_names_cb = get_names_cb

    def createEditor(self, parent: Any, option: Any, index: QModelIndex) -> Any:
        editor = super().createEditor(parent, option, index)
        if isinstance(editor, QLineEdit):
            completer = QCompleter(self.get_names_cb(), editor)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            editor.setCompleter(completer)
        return editor


class BatchByWeightDialog(QDialog):
    """Dialog allowing users to enter discrete component weights and automatically derive formula percentages."""

    def __init__(
        self,
        repository: RecipeRepository,
        service: RecipeService | None = None,
        current_ingredients: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
        current_target_weight: Decimal = Decimal("1000.00"),
        precision: Decimal = Decimal("0.01"),
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.repo = repository
        if service is not None:
            self.service: RecipeService | None = service
        elif repository is not None:
            self.service = RecipeService(repository)
        else:
            self.service = None
        self._precision = precision
        exp = precision.normalize().as_tuple().exponent
        self._precision_decimals = max(0, -exp) if isinstance(exp, int) else 2
        self._initial_target_weight = current_target_weight
        self._initial_ingredients = list(current_ingredients) if current_ingredients else []
        self._is_updating = False
        self._has_user_edits = False
        self._weights_modified = False
        self._structure_modified = False
        self._initial_incomplete = False
        self._total_weight = Decimal("0.00")
        self._manual_target_weight = current_target_weight
        self._resolved_target_weight = current_target_weight

        self.setWindowTitle("Formulate Batch by Weight")
        self.resize(760, 520)
        self.setMinimumWidth(620)

        self._setup_ui()
        self._populate_initial()

    def has_user_edits(self) -> bool:
        """Return True if the user modified any weight, cost, name, or formulation option."""
        return self._has_user_edits

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        header_frame = QFrame()
        header_frame.setFrameShape(QFrame.Shape.StyledPanel)
        h_layout = QVBoxLayout(header_frame)
        h_layout.setContentsMargins(10, 8, 10, 8)
        h_title = QLabel("<b>Batch Formulation by Ingredient Weights</b>")
        h_desc = QLabel(
            "Enter component weights directly in grams. Formula percentages and cumulative batch mass "
            "will be calculated automatically. Component weights must be exact multiples of scale precision."
        )
        h_desc.setStyleSheet("font-size: 8.5pt;")
        h_desc.setWordWrap(True)
        h_layout.addWidget(h_title)
        h_layout.addWidget(h_desc)
        layout.addWidget(header_frame)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Ingredient Name", "Weight (g)", "Unit Cost ($/kg)", "Formula %"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

        header = self.table.horizontalHeader()
        assert header is not None
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)

        self.table.setItemDelegateForColumn(
            0,
            WeightIngredientCompleterDelegate(
                lambda: [it.name for it in self.repo.list_catalog_ingredients()] if self.repo is not None else [],
                self.table,
            ),
        )
        layout.addWidget(self.table)

        tbl_ctrl_layout = QHBoxLayout()
        self.btn_add = QPushButton("+ Add Ingredient")
        self.btn_from_catalog = QPushButton("+ From Catalog...")
        self.btn_remove = QPushButton("− Remove Selected")
        tbl_ctrl_layout.addWidget(self.btn_add)
        tbl_ctrl_layout.addWidget(self.btn_from_catalog)
        tbl_ctrl_layout.addWidget(self.btn_remove)
        tbl_ctrl_layout.addStretch()
        layout.addLayout(tbl_ctrl_layout)

        options_frame = QFrame()
        options_frame.setFrameShape(QFrame.Shape.StyledPanel)
        opt_layout = QHBoxLayout(options_frame)
        opt_layout.setContentsMargins(10, 8, 10, 8)

        self.auto_target_cb = QCheckBox("Set Target Batch Mass to sum of weights")
        self.auto_target_cb.setChecked(True)
        self.auto_target_cb.setStyleSheet("font-weight: bold;")

        self.target_weight_spin = QDoubleSpinBox()
        self.target_weight_spin.setRange(float(self._precision), float(MAX_TARGET_WEIGHT))
        self.target_weight_spin.setDecimals(self._precision_decimals)
        self.target_weight_spin.setValue(float(self._initial_target_weight))
        self.target_weight_spin.setEnabled(False)

        opt_layout.addWidget(self.auto_target_cb)
        opt_layout.addSpacing(10)
        opt_layout.addWidget(QLabel("Target Mass (g):"))
        opt_layout.addWidget(self.target_weight_spin)
        opt_layout.addStretch()
        layout.addWidget(options_frame)

        summary_frame = QFrame()
        summary_frame.setFrameShape(QFrame.Shape.StyledPanel)
        s_layout = QHBoxLayout(summary_frame)
        s_layout.setContentsMargins(10, 8, 10, 8)
        self.summary_label = QLabel("Total Weight: 0.00 g | Formula Total: 0.00%")
        self.summary_label.setStyleSheet("font-weight: bold; font-size: 9.5pt; padding: 4px 8px;")
        s_layout.addWidget(self.summary_label)
        s_layout.addStretch()
        layout.addWidget(summary_frame)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.btn_cancel = QPushButton("Cancel")
        self.btn_apply = QPushButton("Apply to Formula")
        self.btn_apply.setStyleSheet("font-weight: bold; padding: 6px 16px;")
        self.btn_apply.setEnabled(False)

        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addWidget(self.btn_apply)
        layout.addLayout(btn_layout)

        self.btn_add.clicked.connect(lambda: self._add_row())
        self.btn_from_catalog.clicked.connect(self._add_from_catalog)
        self.btn_remove.clicked.connect(self._remove_selected)
        self.auto_target_cb.toggled.connect(self._on_auto_target_toggled)
        self.target_weight_spin.valueChanged.connect(self._on_target_spin_changed)
        self.table.cellChanged.connect(self._on_cell_changed)
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_apply.clicked.connect(self._apply)

    def _should_preserve_initial_percentages(self) -> bool:
        return not self._weights_modified and not (
            self._initial_incomplete and self.auto_target_cb.isChecked()
        )

    def _read_target_spinbox_decimal(self) -> Decimal:
        try:
            return _parse_decimal_input(
                self.target_weight_spin.cleanText(),
                convert_kg=False,
                allow_mass_units=False,
                locale=self.target_weight_spin.locale(),
            )
        except Exception:
            return Decimal(f"{self.target_weight_spin.value():.{self._precision_decimals}f}")

    def _sync_preserved_weights_to_target(self, target_mass: Decimal) -> None:
        """Keep displayed Weight cells and UserRole baselines synced with Target Mass in preserved-% mode."""
        if target_mass <= Decimal("0"):
            return

        row_pcts: list[Decimal] = []
        for r in range(self.table.rowCount()):
            pct_item = self.table.item(r, 3)
            orig_raw = pct_item.data(Qt.ItemDataRole.UserRole) if pct_item is not None else None
            if orig_raw is None:
                return
            row_pcts.append(Decimal(str(orig_raw)))

        if not row_pcts:
            return

        scaled_weights: list[Decimal] = []
        total_pct = sum(row_pcts, Decimal("0"))
        if abs(total_pct - Decimal("100.00")) <= Decimal("0.0001") and all(p > Decimal("0") for p in row_pcts):
            try:
                from tared.core.calculator import compute_batch
                from tared.core.models import Ingredient, Recipe

                names = [
                    (
                        c0.text().strip()
                        if (c0 := self.table.item(r, 0)) is not None and c0.text().strip()
                        else f"Ing_{r}"
                    )
                    for r in range(self.table.rowCount())
                ]
                if len({n.lower() for n in names}) == len(names):
                    temp_recipe = Recipe(
                        name="TargetRescale",
                        ingredients=tuple(
                            Ingredient(name=n, percentage=p) for n, p in zip(names, row_pcts)
                        ),
                    )
                    batch = compute_batch(temp_recipe, target_mass, self._precision)
                    scaled_weights = [it.scaled_weight for it in batch.items]
            except Exception:
                scaled_weights = []

        if not scaled_weights:
            with decimal.localcontext(CALCULATION_CONTEXT):
                for pct in row_pcts:
                    if pct <= Decimal("0"):
                        scaled_weights.append(Decimal("0.00"))
                    else:
                        raw_w = (pct * target_mass) / Decimal("100")
                        scaled_weights.append(
                            (raw_w / self._precision).to_integral_value(rounding=ROUND_HALF_UP) * self._precision
                        )

        self._is_updating = True
        try:
            for r, scaled_w in enumerate(scaled_weights):
                w_item = self.table.item(r, 1)
                if w_item is None:
                    continue
                if scaled_w <= Decimal("0") and row_pcts[r] > Decimal("0"):
                    w_str = _format_decimal_for_edit(f"{Decimal('0'):.{self._precision_decimals}f}")
                else:
                    w_str = _format_decimal_for_edit(_format_weight_input(scaled_w, self._precision_decimals))
                w_item.setText(w_str)
                w_item.setData(Qt.ItemDataRole.UserRole, scaled_w)
        finally:
            self._is_updating = False

    def _on_auto_target_toggled(self, checked: bool) -> None:
        if not self._is_updating:
            self._has_user_edits = True
            if not checked and self._initial_incomplete and not self._weights_modified:
                self.target_weight_spin.blockSignals(True)
                try:
                    self.target_weight_spin.setValue(float(min(self._manual_target_weight, MAX_TARGET_WEIGHT)))
                finally:
                    self.target_weight_spin.blockSignals(False)
        self.target_weight_spin.setEnabled(not checked)
        if not self._is_updating and self._should_preserve_initial_percentages():
            self._sync_preserved_weights_to_target(self._read_target_spinbox_decimal())
        self._recalculate()

    def _on_target_spin_changed(self) -> None:
        if not self._is_updating and not self.auto_target_cb.isChecked():
            self._has_user_edits = True
            self._manual_target_weight = self._read_target_spinbox_decimal()
            if self._should_preserve_initial_percentages():
                self._sync_preserved_weights_to_target(self._manual_target_weight)
        self._recalculate()

    def _populate_initial(self) -> None:
        self._is_updating = True
        try:
            if self._initial_ingredients:
                total_pct = sum(
                    (Decimal(str(it.get("percentage", "0"))) for it in self._initial_ingredients),
                    Decimal("0"),
                )
                scaled_weights_map: list[Decimal] = []
                use_batch_scaling = False
                if abs(total_pct - Decimal("100.00")) <= Decimal("0.0001"):
                    try:
                        from tared.core.calculator import compute_batch
                        from tared.core.models import Ingredient, Recipe

                        test_recipe = Recipe(
                            name="InitialFormulation",
                            ingredients=tuple(
                                Ingredient(
                                    name=str(it.get("name", f"Ing_{i}")).strip() or f"Ing_{i}",
                                    percentage=Decimal(str(it["percentage"])),
                                    cost_per_unit=Decimal(str(it.get("cost_per_unit", "0.00"))),
                                )
                                for i, it in enumerate(self._initial_ingredients)
                            ),
                        )
                        batch = compute_batch(test_recipe, self._initial_target_weight, self._precision)
                        scaled_weights_map = [item.scaled_weight for item in batch.items]
                        use_batch_scaling = True
                    except Exception:
                        use_batch_scaling = False

                for idx, it in enumerate(self._initial_ingredients):
                    name = str(it.get("name", ""))
                    cost = Decimal(str(it.get("cost_per_unit", "0.00")))
                    if use_batch_scaling and idx < len(scaled_weights_map):
                        weight = scaled_weights_map[idx]
                    elif it.get("scaled_weight") is not None and Decimal(str(it["scaled_weight"])) > Decimal("0"):
                        weight = Decimal(str(it["scaled_weight"]))
                    elif it.get("weight") is not None and Decimal(str(it["weight"])) > Decimal("0"):
                        with decimal.localcontext(CALCULATION_CONTEXT):
                            raw_w = Decimal(str(it["weight"]))
                            quot = raw_w / self._precision
                            if quot != quot.to_integral_value():
                                weight = quot.to_integral_value(rounding=ROUND_HALF_UP) * self._precision
                            else:
                                weight = raw_w
                    elif it.get("percentage") is not None and Decimal(str(it["percentage"])) > Decimal("0"):
                        with decimal.localcontext(CALCULATION_CONTEXT):
                            raw_w = (Decimal(str(it["percentage"])) * self._initial_target_weight) / Decimal("100")
                            weight = (
                                (raw_w / self._precision).to_integral_value(rounding=ROUND_HALF_UP) * self._precision
                            )
                    else:
                        weight = Decimal("0.00")

                    orig_pct = (
                        Decimal(str(it["percentage"]))
                        if it.get("percentage") is not None and Decimal(str(it["percentage"])) > Decimal("0")
                        else None
                    )
                    self._insert_row_data(
                        name, weight, cost, initial_percentage=orig_pct, auto_lookup_cost=False
                    )
                if total_pct > Decimal("0") and abs(total_pct - Decimal("100.00")) > Decimal("0.0001"):
                    self._initial_incomplete = True
                    self.auto_target_cb.blockSignals(True)
                    try:
                        self.auto_target_cb.setChecked(False)
                        self.target_weight_spin.setEnabled(True)
                    finally:
                        self.auto_target_cb.blockSignals(False)
            else:
                self._insert_row_data("Ingredient 1", Decimal("0.00"), Decimal("0.00"), auto_lookup_cost=False)
        finally:
            self._is_updating = False

        self._recalculate()

    def _insert_row_data(
        self,
        name: str,
        weight: Decimal,
        cost: Decimal,
        initial_percentage: Decimal | None = None,
        auto_lookup_cost: bool = True,
    ) -> None:
        clean_name = name.strip()
        if auto_lookup_cost and cost <= Decimal("0.00") and clean_name and self.service is not None:
            cat_cost = self.service.get_catalog_price(clean_name)
            if cat_cost is not None and cat_cost > Decimal("0"):
                cost = cat_cost

        row = self.table.rowCount()
        self.table.insertRow(row)

        name_item = QTableWidgetItem(clean_name)
        if weight <= Decimal("0") and initial_percentage is not None and initial_percentage > Decimal("0"):
            weight_str = _format_decimal_for_edit(f"{Decimal('0'):.{self._precision_decimals}f}")
        else:
            weight_str = _format_decimal_for_edit(_format_weight_input(weight, self._precision_decimals))
        weight_item = QTableWidgetItem(weight_str)
        weight_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        weight_item.setData(Qt.ItemDataRole.UserRole, weight)

        cost_item = QTableWidgetItem(_format_decimal_for_edit(_format_unit_cost(cost)))
        cost_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        pct_item = QTableWidgetItem("—")
        pct_item.setFlags(pct_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        pct_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if initial_percentage is not None:
            pct_item.setData(Qt.ItemDataRole.UserRole, initial_percentage)

        self.table.setItem(row, 0, name_item)
        self.table.setItem(row, 1, weight_item)
        self.table.setItem(row, 2, cost_item)
        self.table.setItem(row, 3, pct_item)

    def _add_row(self, name: str = "", weight: Decimal = Decimal("0.00"), cost: Decimal = Decimal("0.00")) -> None:
        self._has_user_edits = True
        self._structure_modified = True
        self._weights_modified = True
        self._is_updating = True
        try:
            self._insert_row_data(name, weight, cost)
        finally:
            self._is_updating = False
        self._recalculate()
        last_row = self.table.rowCount() - 1
        self.table.selectRow(last_row)
        if self.isVisible():
            col_to_edit = 0 if not name else 1
            target_item = self.table.item(last_row, col_to_edit)
            if target_item:
                self.table.editItem(target_item)

    def _add_from_catalog(self) -> None:
        dialog = CatalogPickerDialog(self.repo, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_items:
            self._has_user_edits = True
            self._structure_modified = True
            self._weights_modified = True
            self._is_updating = True
            try:
                for item in dialog.selected_items:
                    self._insert_row_data(item.name, Decimal("0.00"), item.cost_per_unit)
            finally:
                self._is_updating = False
            self._recalculate()

    def _remove_selected(self) -> None:
        sel_model = self.table.selectionModel()
        if sel_model is None:
            return
        rows = sorted({idx.row() for idx in sel_model.selectedRows()}, reverse=True)
        if not rows:
            return
        self._has_user_edits = True
        self._structure_modified = True
        self._weights_modified = True
        self._is_updating = True
        try:
            for r in rows:
                self.table.removeRow(r)
        finally:
            self._is_updating = False
        self._recalculate()

    def _on_cell_changed(self, row: int, col: int) -> None:
        if self._is_updating:
            return

        self._has_user_edits = True

        if col == 0:
            name_item = self.table.item(row, 0)
            if name_item:
                name = name_item.text().strip()
                cost_item = self.table.item(row, 2)
                cur_cost_str = cost_item.text().strip() if cost_item else "0.00"
                is_zero_cost = cur_cost_str in ("", "0", "0.00", _format_decimal_for_edit("0.00"))
                if not is_zero_cost and cur_cost_str:
                    try:
                        is_zero_cost = (
                            _parse_decimal_input(cur_cost_str, convert_kg=False, allow_mass_units=False)
                            == Decimal("0")
                        )
                    except Exception:
                        is_zero_cost = False
                if is_zero_cost and self.service is not None:
                    cat_cost = self.service.get_catalog_price(name)
                    if cat_cost is not None and cat_cost > Decimal("0"):
                        self._is_updating = True
                        try:
                            if cost_item:
                                cost_item.setText(_format_decimal_for_edit(_format_unit_cost(cat_cost)))
                        finally:
                            self._is_updating = False
        elif col == 1:
            self._weights_modified = self._structure_modified or self._has_any_weight_changed()

        self._recalculate()

    def _has_any_weight_changed(self) -> bool:
        for r in range(self.table.rowCount()):
            w_item = self.table.item(r, 1)
            if w_item is None:
                continue
            init_w = w_item.data(Qt.ItemDataRole.UserRole)
            if init_w is None:
                return True
            w_str = w_item.text().strip()
            try:
                w_dec = (
                    _parse_decimal_input(w_str, convert_kg=True, allow_mass_units=True)
                    if w_str
                    else Decimal("0")
                )
            except Exception:
                return True
            if w_dec != init_w:
                return True
        return False

    def _recalculate(self) -> None:
        total_w = Decimal("0")
        row_weights: list[Decimal] = []
        weight_input_valid = True
        weight_error_msg = ""
        cost_input_valid = True
        cost_error_msg = ""
        precision_valid = True
        precision_error_msg = ""
        zero_weight_msg = ""

        for r in range(self.table.rowCount()):
            w_item = self.table.item(r, 1)
            w_str = w_item.text().strip() if w_item else ""
            if not w_str:
                w_dec = Decimal("0")
            else:
                try:
                    w_dec = _parse_decimal_input(w_str, convert_kg=True, allow_mass_units=True)
                    if w_dec < Decimal("0"):
                        weight_input_valid = False
                        if not weight_error_msg:
                            weight_error_msg = f"Row {r + 1} weight ({w_dec} g) cannot be negative"
                        w_dec = Decimal("0")
                except Exception:
                    weight_input_valid = False
                    if not weight_error_msg:
                        weight_error_msg = f"Row {r + 1} has invalid weight '{w_str}'"
                    w_dec = Decimal("0")
            row_weights.append(w_dec)
            total_w += w_dec

            c_item = self.table.item(r, 2)
            c_str = c_item.text().strip() if c_item else ""
            if c_str:
                try:
                    c_dec = _parse_decimal_input(c_str, convert_kg=False, allow_mass_units=False)
                    if c_dec < Decimal("0"):
                        cost_input_valid = False
                        if not cost_error_msg:
                            cost_error_msg = f"Row {r + 1} unit cost ({c_dec}) cannot be negative"
                    elif c_dec > MAX_UNIT_COST:
                        cost_input_valid = False
                        if not cost_error_msg:
                            cost_error_msg = (
                                f"Row {r + 1} unit cost ({c_dec}) exceeds maximum limit ({MAX_UNIT_COST})"
                            )
                except Exception:
                    cost_input_valid = False
                    if not cost_error_msg:
                        cost_error_msg = f"Row {r + 1} has invalid unit cost '{c_str}'"

            if w_dec > Decimal("0"):
                if w_dec < self._precision:
                    precision_valid = False
                    if not precision_error_msg:
                        precision_error_msg = (
                            f"Row {r + 1} ({w_dec} g) is smaller than scale precision ({self._precision} g)"
                        )
                else:
                    quot = w_dec / self._precision
                    if quot != quot.to_integral_value():
                        precision_valid = False
                        if not precision_error_msg:
                            precision_error_msg = (
                                f"Row {r + 1} ({w_dec} g) is not a multiple of scale precision ({self._precision} g)"
                            )

        self._total_weight = total_w

        if self.auto_target_cb.isChecked():
            target_val = total_w if total_w > Decimal("0") else self._initial_target_weight
            self.target_weight_spin.blockSignals(True)
            try:
                self.target_weight_spin.setValue(float(min(target_val, MAX_TARGET_WEIGHT)))
            finally:
                self.target_weight_spin.blockSignals(False)
            self._resolved_target_weight = target_val
        else:
            self._resolved_target_weight = self._read_target_spinbox_decimal()

            if self._resolved_target_weight > Decimal("0"):
                t_quot = self._resolved_target_weight / self._precision
                if t_quot != t_quot.to_integral_value():
                    precision_valid = False
                    if not precision_error_msg:
                        precision_error_msg = (
                            f"Target mass ({self._resolved_target_weight} g) is not a multiple of scale precision"
                        )

        self._is_updating = True
        computed_total_pct = Decimal("0")
        preserve_pct = self._should_preserve_initial_percentages()
        try:
            with decimal.localcontext(CALCULATION_CONTEXT):
                for r in range(self.table.rowCount()):
                    w_dec = row_weights[r]
                    pct_item = self.table.item(r, 3)
                    if preserve_pct and pct_item and pct_item.data(Qt.ItemDataRole.UserRole) is not None:
                        orig_pct = Decimal(str(pct_item.data(Qt.ItemDataRole.UserRole)))
                        computed_total_pct += orig_pct
                        txt = f"{format_percentage(orig_pct)}%"
                    elif total_w > Decimal("0") and w_dec > Decimal("0"):
                        pct = (w_dec / total_w) * Decimal("100")
                        computed_total_pct += pct
                        txt = f"{format_percentage(pct)}%"
                    else:
                        txt = "—"
                    if pct_item:
                        pct_item.setText(txt)
        finally:
            self._is_updating = False

        names_list = [
            c0.text().strip().lower()
            for r in range(self.table.rowCount())
            if (c0 := self.table.item(r, 0)) is not None and c0.text().strip()
        ]
        has_duplicates = len(names_list) != len(set(names_list))

        for r, w in enumerate(row_weights):
            if w == Decimal("0"):
                pct_cell = self.table.item(r, 3)
                is_preserved = (
                    preserve_pct
                    and pct_cell is not None
                    and pct_cell.data(Qt.ItemDataRole.UserRole) is not None
                    and Decimal(str(pct_cell.data(Qt.ItemDataRole.UserRole))) > Decimal("0")
                )
                w_cell = self.table.item(r, 1)
                w_text = w_cell.text().strip() if w_cell else ""
                if not is_preserved and (w_text or total_w > Decimal("0")):
                    zero_weight_msg = f"Row {r + 1} weight must be greater than 0"
                    break

        target_limit_valid = True
        target_limit_error_msg = ""
        if self._resolved_target_weight > MAX_TARGET_WEIGHT:
            target_limit_valid = False
            target_limit_error_msg = (
                f"Target mass ({self._resolved_target_weight:.{self._precision_decimals}f} g) "
                f"exceeds maximum limit ({MAX_TARGET_WEIGHT} g)"
            )

        if not weight_input_valid:
            status_text = f"Total Batch Weight: {total_w:.{self._precision_decimals}f} g | ⚠ {weight_error_msg}"
        elif not cost_input_valid:
            status_text = f"Total Batch Weight: {total_w:.{self._precision_decimals}f} g | ⚠ {cost_error_msg}"
        elif not precision_valid:
            status_text = f"Total Batch Weight: {total_w:.{self._precision_decimals}f} g | ⚠ {precision_error_msg}"
        elif not target_limit_valid:
            status_text = f"Total Batch Weight: {total_w:.{self._precision_decimals}f} g | ⚠ {target_limit_error_msg}"
        elif zero_weight_msg:
            status_text = f"Total Batch Weight: {total_w:.{self._precision_decimals}f} g | ⚠ {zero_weight_msg}"
        elif has_duplicates:
            status_text = (
                f"Total Batch Weight: {total_w:.{self._precision_decimals}f} g | "
                "⚠ Duplicate ingredient names detected"
            )
        elif self.auto_target_cb.isChecked():
            status_text = (
                f"Total Batch Weight: {total_w:.{self._precision_decimals}f} g | "
                f"Formula Total: {format_percentage(computed_total_pct)}%"
            )
        else:
            status_text = (
                f"Total Batch Weight: {total_w:.{self._precision_decimals}f} g | "
                f"Target Mass: {self._resolved_target_weight:.{self._precision_decimals}f} g (Proportional Scaling) | "
                f"Formula Total: {format_percentage(computed_total_pct)}%"
            )
        has_error = (
            not weight_input_valid
            or not cost_input_valid
            or not precision_valid
            or not target_limit_valid
            or bool(zero_weight_msg)
            or has_duplicates
        )
        if has_error:
            self.summary_label.setStyleSheet(
                "color: #842029; background-color: #f8d7da; font-weight: bold; "
                "font-size: 9.5pt; padding: 4px 8px; border-radius: 4px;"
            )
        else:
            self.summary_label.setStyleSheet("font-weight: bold; font-size: 9.5pt; padding: 4px 8px;")
        self.summary_label.setText(status_text)

        def _row_weight_ready(row_idx: int, w: Decimal) -> bool:
            if w >= self._precision:
                return True
            if w == Decimal("0") and preserve_pct:
                pct_cell = self.table.item(row_idx, 3)
                if pct_cell and pct_cell.data(Qt.ItemDataRole.UserRole) is not None:
                    return Decimal(str(pct_cell.data(Qt.ItemDataRole.UserRole))) > Decimal("0")
            return False

        all_valid = (
            self.table.rowCount() > 0
            and total_w > Decimal("0")
            and weight_input_valid
            and cost_input_valid
            and precision_valid
            and target_limit_valid
            and not has_duplicates
            and all(_row_weight_ready(r, w) for r, w in enumerate(row_weights))
            and all(
                (c0 := self.table.item(r, 0)) is not None and bool(c0.text().strip())
                for r in range(self.table.rowCount())
            )
        )
        self.btn_apply.setEnabled(all_valid)

    def _apply(self) -> None:
        if self.table.rowCount() == 0:
            QMessageBox.warning(self, "Empty Formula", "Please add at least one ingredient.")
            return

        seen_names = set()
        for r in range(self.table.rowCount()):
            c0 = self.table.item(r, 0)
            name = c0.text().strip() if c0 is not None else ""
            if not name:
                QMessageBox.warning(self, "Validation Error", f"Row {r + 1} has an empty ingredient name.")
                return
            if name.lower() in seen_names:
                QMessageBox.warning(self, "Validation Error", f"Row {r + 1} has duplicate ingredient name '{name}'.")
                return
            seen_names.add(name.lower())

            c1 = self.table.item(r, 1)
            w_str = c1.text().strip() if c1 is not None else ""
            pct_cell = self.table.item(r, 3)
            is_preserved_trace = (
                self._should_preserve_initial_percentages()
                and pct_cell is not None
                and pct_cell.data(Qt.ItemDataRole.UserRole) is not None
                and Decimal(str(pct_cell.data(Qt.ItemDataRole.UserRole))) > Decimal("0")
            )
            try:
                if not w_str:
                    w_dec = Decimal("0")
                else:
                    w_dec = _parse_decimal_input(w_str, convert_kg=True, allow_mass_units=True)
                if w_dec == Decimal("0") and is_preserved_trace:
                    pass
                elif w_dec <= Decimal("0"):
                    raise ValueError("Must be positive")
                if w_dec > Decimal("0"):
                    if w_dec < self._precision:
                        msg = (
                            f"Row {r + 1} ('{name}') weight ({w_dec} g) is smaller "
                            f"than scale precision ({self._precision} g)."
                        )
                        QMessageBox.warning(self, "Validation Error", msg)
                        return
                    quot = w_dec / self._precision
                    if quot != quot.to_integral_value():
                        msg = (
                            f"Row {r + 1} ('{name}') weight ({w_dec} g) is not an exact "
                            f"multiple of scale precision ({self._precision} g)."
                        )
                        QMessageBox.warning(self, "Validation Error", msg)
                        return
            except Exception as err:
                QMessageBox.warning(self, "Validation Error", f"Row {r + 1} ('{name}') has invalid weight: {err}")
                return

            c2 = self.table.item(r, 2)
            cost_str = c2.text().strip() if c2 is not None else ""
            try:
                c_dec = (
                    _parse_decimal_input(cost_str, convert_kg=False, allow_mass_units=False)
                    if cost_str
                    else Decimal("0.00")
                )
                if c_dec < Decimal("0"):
                    raise ValueError("must be non-negative")
                if c_dec > MAX_UNIT_COST:
                    raise ValueError(f"cannot exceed {MAX_UNIT_COST}")
            except Exception as err:
                QMessageBox.warning(self, "Validation Error", f"Row {r + 1} ('{name}') has invalid unit cost: {err}")
                return

        self._resolved_target_weight = self._read_target_spinbox_decimal()

        if self.get_target_weight() > MAX_TARGET_WEIGHT:
            QMessageBox.warning(
                self,
                "Validation Error",
                f"Target Mass ({self.get_target_weight()} g) exceeds maximum limit ({MAX_TARGET_WEIGHT} g).",
            )
            return

        if not self.auto_target_cb.isChecked():
            t_quot = self._resolved_target_weight / self._precision
            if t_quot != t_quot.to_integral_value():
                msg = (
                    f"Target Mass ({self._resolved_target_weight} g) is not an exact "
                    f"multiple of scale precision ({self._precision} g)."
                )
                QMessageBox.warning(self, "Validation Error", msg)
                return

        self.accept()

    def get_ingredients(self) -> list[dict[str, Any]]:
        """Return validated ingredient definitions including discrete weights and unit costs."""
        items: list[dict[str, Any]] = []
        for r in range(self.table.rowCount()):
            c0 = self.table.item(r, 0)
            c1 = self.table.item(r, 1)
            c2 = self.table.item(r, 2)
            name = c0.text().strip() if c0 is not None else ""
            w_str = c1.text().strip() if c1 is not None else ""
            cost_str = c2.text().strip() if c2 is not None else "0.00"
            try:
                w_dec = _parse_decimal_input(w_str, convert_kg=True, allow_mass_units=True)
            except Exception:
                w_dec = Decimal("0.00")
            try:
                cost_dec = _parse_decimal_input(cost_str, convert_kg=False, allow_mass_units=False)
            except Exception:
                cost_dec = Decimal("0.00")

            item_dict: dict[str, Any] = {
                "name": name,
                "weight": w_dec,
                "cost_per_unit": cost_dec,
            }

            pct_item = self.table.item(r, 3)
            if (
                pct_item
                and pct_item.data(Qt.ItemDataRole.UserRole) is not None
                and self._should_preserve_initial_percentages()
            ):
                item_dict["percentage"] = pct_item.data(Qt.ItemDataRole.UserRole)

            items.append(item_dict)
        return items

    def get_target_weight(self) -> Decimal:
        """Return the resolved Target Batch Mass in grams."""
        if self.auto_target_cb.isChecked() and self._total_weight > Decimal("0"):
            return self._total_weight
        return self._resolved_target_weight


# Convenient alias
WeightEntryDialog = BatchByWeightDialog
