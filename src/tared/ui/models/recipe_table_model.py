"""PyQt6 QAbstractTableModel adapting Recipe and BatchResult for real-time reactivity."""

import decimal
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

try:
    from PyQt6.QtCore import QAbstractTableModel, QLocale, QModelIndex, Qt, pyqtSignal
except ImportError:

    class QAbstractTableModel:
        pass  # type: ignore

    class QModelIndex:
        pass  # type: ignore

    class QLocale:
        pass  # type: ignore

    class Qt:  # type: ignore
        class AlignmentFlag:
            AlignLeft = 1
            AlignVCenter = 2
            AlignRight = 4

        class ItemFlag:
            NoItemFlags = 0
            ItemIsEnabled = 1
            ItemIsSelectable = 2
            ItemIsEditable = 4

        class ItemDataRole:
            DisplayRole = 0
            EditRole = 2
            TextAlignmentRole = 4

        class Orientation:
            Horizontal = 1
            Vertical = 2

    def pyqtSignal(*args, **kwargs):  # type: ignore
        class _Signal:
            def emit(self, *a, **k):
                pass

            def connect(self, *a, **k):
                pass

        return _Signal()


from tared.core.calculator import (  # noqa: E402
    CALCULATION_CONTEXT,
    IncompatibleTargetPrecisionError,
    PercentageSumError,
    RecipeCalculationError,
    compute_batch,
)
from tared.core.formatting import determine_cost_precision, format_currency, format_percentage  # noqa: E402
from tared.core.models import BatchResult, Ingredient, Recipe  # noqa: E402


def _parse_decimal_input(
    value: Any,
    convert_kg: bool = False,
    allow_mass_units: bool = False,
) -> Decimal:
    raw = str(value).strip().replace("%", "").replace("$", "").strip()
    if not raw:
        raise ValueError("Empty numeric input")

    has_unit = False
    is_kg = False
    matched_suffix = ""
    raw_lower = raw.lower()
    if raw_lower.endswith("kg"):
        matched_suffix = raw[-2:]
        raw = raw[:-2].strip()
        is_kg = True
        has_unit = True
    elif raw_lower.endswith("g"):
        matched_suffix = raw[-1:]
        raw = raw[:-1].strip()
        has_unit = True

    if has_unit and not (convert_kg or allow_mass_units):
        raise ValueError(f"Mass unit suffix '{matched_suffix}' is not permitted for this field.")

    if not raw:
        raise ValueError("Empty numeric input")

    if any(c in raw for c in ("e", "E")) and not any(c in raw for c in (",", " ")):
        try:
            dec = Decimal(raw)
            if not dec.is_finite():
                raise ValueError("Non-finite numeric input")
            if is_kg and convert_kg:
                dec = dec * Decimal("1000")
            return dec
        except Exception as err:
            raise ValueError(f"Malformed numeric format '{value}'") from err

    try:
        locale = QLocale()
        group_sep = locale.groupSeparator()
        dec_point = locale.decimalPoint()
    except Exception:
        group_sep = ","
        dec_point = "."

    if not group_sep or not dec_point or group_sep == dec_point:
        group_sep = ","
        dec_point = "."

    if raw.count(dec_point) > 1:
        raise ValueError(f"Multiple decimal separators in '{value}'")

    has_dec = dec_point in raw
    has_group = group_sep in raw

    if has_dec and has_group:
        dec_idx = raw.index(dec_point)
        last_group_idx = raw.rindex(group_sep)
        if last_group_idx > dec_idx:
            raise ValueError(f"Group separator after decimal separator in '{value}'")
        int_str, frac_str = raw.split(dec_point, 1)
        if frac_str and not frac_str.isdigit():
            raise ValueError(f"Malformed fractional part in '{value}'")

        sign = "-" if int_str.startswith("-") else ("+" if int_str.startswith("+") else "")
        clean_int = int_str[len(sign) :] if sign else int_str
        parts = clean_int.split(group_sep)
        if not (1 <= len(parts[0]) <= 3 and parts[0].isdigit()):
            raise ValueError(f"Invalid grouping in integer part of '{value}'")
        for part in parts[1:]:
            if len(part) != 3 or not part.isdigit():
                raise ValueError(f"Invalid grouping in integer part of '{value}'")
        normalized = f"{sign}{''.join(parts)}.{frac_str if frac_str else '0'}"

    elif has_dec:
        int_str, frac_str = raw.split(dec_point, 1)
        if frac_str and not frac_str.isdigit():
            raise ValueError(f"Malformed fractional part in '{value}'")
        sign = "-" if int_str.startswith("-") else ("+" if int_str.startswith("+") else "")
        clean_int = int_str[len(sign) :] if sign else int_str
        if clean_int and not clean_int.isdigit():
            raise ValueError(f"Malformed integer part in '{value}'")
        int_digits = clean_int if clean_int else "0"
        normalized = f"{sign}{int_digits}.{frac_str if frac_str else '0'}"

    elif has_group:
        sign = "-" if raw.startswith("-") else ("+" if raw.startswith("+") else "")
        clean_raw = raw[len(sign) :] if sign else raw
        parts = clean_raw.split(group_sep)
        if not (1 <= len(parts[0]) <= 3 and parts[0].isdigit()):
            raise ValueError(f"Invalid grouping in '{value}'")
        for part in parts[1:]:
            if len(part) != 3 or not part.isdigit():
                raise ValueError(f"Invalid grouping in '{value}'")
        normalized = f"{sign}{''.join(parts)}"

    else:
        normalized = raw

    try:
        dec = Decimal(normalized)
        if not dec.is_finite():
            raise ValueError("Non-finite numeric input")
        if is_kg and convert_kg:
            dec = dec * Decimal("1000")
        return dec
    except Exception as err:
        raise ValueError(f"Malformed numeric format '{value}'") from err


COLUMNS = [
    ("Ingredient", Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
    ("Formula %", Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
    ("Unit Cost ($/kg)", Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
    ("Scaled Weight", Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
    ("Cumulative Tare", Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
    ("Line Cost ($)", Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
]


class RecipeTableModel(QAbstractTableModel):
    """Reactive table model synchronizing live ingredient inputs with batch calculations."""

    validation_changed = pyqtSignal(bool, str)
    batch_updated = pyqtSignal(object)
    edit_rejected = pyqtSignal(str)

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._recipe_id: str = ""
        self._recipe_name: str = "New Formula"
        self._recipe_description: str = ""
        self._recipe_version: int = 1
        self._recipe_updated_at: str = ""
        self._is_draft: bool = False
        self._ingredients: list[dict[str, Any]] = []
        self._next_original_order: int = 0
        self._target_weight: Decimal = Decimal("1000.00")
        self._precision: Decimal = Decimal("0.01")
        self._cost_precision: int = 2
        self._current_batch: BatchResult | None = None
        self._is_valid: bool = False
        self._catalog_lookup: Callable[[str], Decimal | None] | None = None

    def set_catalog_lookup(self, lookup: Callable[[str], Decimal | None] | None) -> None:
        self._catalog_lookup = lookup

    @property
    def current_batch(self) -> BatchResult | None:
        return self._current_batch

    @property
    def recipe_id(self) -> str:
        return self._recipe_id

    @property
    def recipe_version(self) -> int:
        return self._recipe_version

    @property
    def recipe_updated_at(self) -> str:
        return self._recipe_updated_at

    @property
    def is_draft(self) -> bool:
        return self._is_draft

    @property
    def precision(self) -> Decimal:
        return self._precision

    @property
    def cost_precision(self) -> int:
        return self._cost_precision

    @property
    def precision_decimals(self) -> int:
        exp = self._precision.as_tuple().exponent
        return max(0, -exp) if isinstance(exp, int) else 2

    @property
    def ingredients_data(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(item) for item in self._ingredients)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent is not None and parent.isValid():
            return 0
        return len(self._ingredients)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent is not None and parent.isValid():
            return 0
        return len(COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = 0) -> Any:
        if orientation == Qt.Orientation.Horizontal and role in (Qt.ItemDataRole.DisplayRole, 0):
            if 0 <= section < len(COLUMNS):
                return COLUMNS[section][0]
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid() or index.parent().isValid():
            return Qt.ItemFlag.NoItemFlags

        base_flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() in (0, 1, 2, 3):
            return base_flags | Qt.ItemFlag.ItemIsEditable
        return base_flags

    def data(self, index: QModelIndex, role: int = 0) -> Any:
        if not index.isValid() or index.parent().isValid():
            return None
        if not (0 <= index.row() < len(self._ingredients)) or not (0 <= index.column() < len(COLUMNS)):
            return None

        row = index.row()
        col = index.column()
        item = self._ingredients[row]

        if role in (Qt.ItemDataRole.TextAlignmentRole, 4):
            return int(COLUMNS[col][1])

        if role in (Qt.ItemDataRole.EditRole, 2):
            if col == 0:
                return item["name"]
            if col == 1:
                return format_percentage(item["percentage"])
            if col == 2:
                return str(item["cost_per_unit"])
            if col == 3:
                if self._current_batch is not None and row < len(self._current_batch.items):
                    return f"{self._current_batch.items[row].scaled_weight:.{self.precision_decimals}f}"
                if item.get("weight") is not None and item["weight"] > Decimal("0"):
                    return f"{item['weight']:.{self.precision_decimals}f}"
                if item["percentage"] > Decimal("0"):
                    raw_w = (item["percentage"] * self._target_weight) / Decimal("100")
                    return f"{raw_w:.{self.precision_decimals}f}"
                return "0.00"
            return None

        if role in (Qt.ItemDataRole.DisplayRole, 0):
            if col == 0:
                return item["name"]
            if col == 1:
                return f"{format_percentage(item['percentage'])}%"
            if col == 2:
                return format_currency(item["cost_per_unit"])

            if self._current_batch is not None and row < len(self._current_batch.items):
                b_item = self._current_batch.items[row]
                if col == 3:
                    return f"{b_item.scaled_weight:.{self.precision_decimals}f}"
                if col == 4:
                    return f"{b_item.cumulative_tare:.{self.precision_decimals}f}"
                if col == 5:
                    return format_currency(b_item.cost, precision=self._cost_precision)

            if col == 3:
                if item.get("weight") is not None and item["weight"] > Decimal("0"):
                    return f"{item['weight']:.{self.precision_decimals}f}"
                if item["percentage"] > Decimal("0"):
                    raw_w = (item["percentage"] * self._target_weight) / Decimal("100")
                    return f"{raw_w:.{self.precision_decimals}f}"

            return "—"

        return None

    def setData(self, index: QModelIndex, value: Any, role: int = 2) -> bool:
        if not index.isValid() or index.parent().isValid() or role not in (Qt.ItemDataRole.EditRole, 2):
            return False
        if not (0 <= index.row() < len(self._ingredients)) or not (0 <= index.column() < len(COLUMNS)):
            return False

        row = index.row()
        col = index.column()
        col_start = col
        col_end = col

        try:
            if col == 0:
                val_str = str(value).strip()
                if not val_str:
                    self.edit_rejected.emit("Ingredient name cannot be empty.")
                    return False
                self._ingredients[row]["name"] = val_str
                if self._ingredients[row]["cost_per_unit"] == Decimal("0.00") and self._catalog_lookup:
                    default_cost = self._catalog_lookup(val_str)
                    if default_cost is not None and default_cost > Decimal("0"):
                        self._ingredients[row]["cost_per_unit"] = default_cost
                        col_end = 2
            elif col == 1:
                raw_str = str(value).strip().replace("%", "").strip()
                current_pct = self._ingredients[row]["percentage"]
                if raw_str == format_percentage(current_pct) or raw_str == str(current_pct):
                    return True
                val_dec = _parse_decimal_input(value, convert_kg=False, allow_mass_units=False)
                if val_dec < Decimal("0") or val_dec > Decimal("100.00"):
                    self.edit_rejected.emit(f"Percentage must be between 0.00% and 100.00% (got {val_dec}%).")
                    return False
                if val_dec == current_pct:
                    return True
                with decimal.localcontext(CALCULATION_CONTEXT):
                    self._ingredients[row]["percentage"] = val_dec
                    self._ingredients[row]["weight"] = (val_dec * self._target_weight) / Decimal("100")
                col_end = 3
            elif col == 2:
                raw_str = str(value).strip().replace("$", "").strip()
                current_cost = self._ingredients[row]["cost_per_unit"]
                if raw_str == str(current_cost):
                    return True
                val_dec = _parse_decimal_input(value, convert_kg=False, allow_mass_units=False)
                if val_dec < Decimal("0"):
                    self.edit_rejected.emit(f"Unit cost cannot be negative (got {val_dec}).")
                    return False
                if val_dec == current_cost:
                    return True
                self._ingredients[row]["cost_per_unit"] = val_dec
            elif col == 3:
                val_dec = _parse_decimal_input(value, convert_kg=True, allow_mass_units=True)
                if val_dec < Decimal("0"):
                    self.edit_rejected.emit(f"Weight cannot be negative (got {val_dec} g).")
                    return False
                if self._target_weight <= Decimal("0"):
                    self.edit_rejected.emit("Target batch mass must be greater than 0.")
                    return False
                if val_dec > self._target_weight:
                    self.edit_rejected.emit(
                        f"Weight ({val_dec} g) exceeds Target Batch Mass ({self._target_weight} g). "
                        f"Increase Target Batch Mass first or use 'Batch by Weight...'."
                    )
                    return False
                quot = val_dec / self._precision
                if quot != quot.to_integral_value():
                    self.edit_rejected.emit(
                        f"Weight ({val_dec} g) must be an exact multiple of scale precision ({self._precision} g)."
                    )
                    return False
                with decimal.localcontext(CALCULATION_CONTEXT):
                    self._ingredients[row]["weight"] = val_dec
                    computed_pct = (val_dec / self._target_weight) * Decimal("100")
                    self._ingredients[row]["percentage"] = computed_pct
                col_start = 1
                col_end = 3
            else:
                return False
        except (InvalidOperation, ValueError) as err:
            col_name = COLUMNS[col][0]
            self.edit_rejected.emit(f"Invalid input '{value}' for {col_name}: {err}")
            return False

        self.dataChanged.emit(
            self.index(row, col_start),
            self.index(row, col_end),
            [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole],
        )
        self.recalculate()
        return True

    def set_live_metadata(self, name: str, description: str) -> None:
        clean_name = name.strip() or "Untitled Recipe"
        if clean_name != self._recipe_name or description != self._recipe_description:
            self._recipe_name = clean_name
            self._recipe_description = description
            self.recalculate()

    def new_draft(self, name: str = "New Formulation", description: str = "") -> None:
        self.beginResetModel()
        self._recipe_id = ""
        self._recipe_name = name
        self._recipe_description = description
        self._recipe_version = 1
        self._recipe_updated_at = ""
        self._is_draft = False
        self._ingredients = []
        self._next_original_order = 0
        self.endResetModel()
        self.recalculate()

    def load_recipe(self, recipe: Recipe, auto_sort: bool = False) -> None:
        self.beginResetModel()
        self._recipe_id = recipe.id
        self._recipe_name = recipe.name
        self._recipe_description = recipe.description
        self._recipe_version = recipe.version
        self._recipe_updated_at = recipe.updated_at
        self._is_draft = recipe.is_draft
        self._ingredients = [
            {
                "name": ing.name,
                "percentage": ing.percentage,
                "cost_per_unit": ing.cost_per_unit,
                "weight": (ing.percentage * self._target_weight) / Decimal("100"),
                "original_order": idx,
            }
            for idx, ing in enumerate(recipe.ingredients)
        ]
        self._next_original_order = len(recipe.ingredients)
        if auto_sort:
            self._ingredients.sort(key=lambda x: x["percentage"], reverse=True)
        self.endResetModel()
        self.recalculate()

    def set_ingredients_from_weights(
        self,
        items: list[dict[str, Any]],
        target_weight: Decimal | None = None,
    ) -> None:
        """Set ingredients and calculate exact formula percentages from discrete ingredient weights."""
        if target_weight is not None and target_weight > Decimal("0"):
            self._target_weight = target_weight

        total_weight = sum((Decimal(str(it["weight"])) for it in items), Decimal("0"))
        if total_weight <= Decimal("0"):
            raise ValueError("Total weight of ingredients must be greater than 0.")

        self.beginResetModel()
        self._ingredients = []
        with decimal.localcontext(CALCULATION_CONTEXT):
            for idx, it in enumerate(items):
                w = Decimal(str(it["weight"]))
                has_pct = "percentage" in it and it["percentage"] is not None
                if has_pct and Decimal(str(it["percentage"])) > Decimal("0"):
                    pct = Decimal(str(it["percentage"]))
                else:
                    pct = (w / total_weight) * Decimal("100")
                cost = Decimal(str(it.get("cost_per_unit", "0.00")))
                self._ingredients.append(
                    {
                        "name": str(it["name"]).strip(),
                        "percentage": pct,
                        "cost_per_unit": cost,
                        "weight": w,
                        "original_order": idx,
                    }
                )
        self._next_original_order = len(self._ingredients)
        self.endResetModel()
        self.recalculate()

    def move_ingredient_up(self, row: int) -> bool:
        if row <= 0 or row >= len(self._ingredients):
            return False

        self.beginResetModel()
        self._ingredients[row - 1], self._ingredients[row] = self._ingredients[row], self._ingredients[row - 1]
        for idx, item in enumerate(self._ingredients):
            item["original_order"] = idx
        self._next_original_order = len(self._ingredients)
        self.endResetModel()
        self.recalculate()
        return True

    def move_ingredient_down(self, row: int) -> bool:
        if row < 0 or row >= len(self._ingredients) - 1:
            return False

        self.beginResetModel()
        self._ingredients[row], self._ingredients[row + 1] = self._ingredients[row + 1], self._ingredients[row]
        for idx, item in enumerate(self._ingredients):
            item["original_order"] = idx
        self._next_original_order = len(self._ingredients)
        self.endResetModel()
        self.recalculate()
        return True

    def sort_by_percentage(self, descending: bool = True) -> bool:
        prev_sequence = [id(item) for item in self._ingredients]
        self.beginResetModel()
        self._ingredients.sort(key=lambda x: x["percentage"], reverse=descending)
        self.endResetModel()
        self.recalculate()
        return [id(item) for item in self._ingredients] != prev_sequence

    def sort_by_name(self, ascending: bool = True) -> bool:
        prev_sequence = [id(item) for item in self._ingredients]
        self.beginResetModel()
        self._ingredients.sort(key=lambda x: x["name"].lower(), reverse=not ascending)
        self.endResetModel()
        self.recalculate()
        return [id(item) for item in self._ingredients] != prev_sequence

    def restore_original_order(self) -> bool:
        prev_sequence = [id(item) for item in self._ingredients]
        self.beginResetModel()
        self._ingredients.sort(key=lambda x: x.get("original_order", 0))
        self.endResetModel()
        self.recalculate()
        return [id(item) for item in self._ingredients] != prev_sequence

    def get_recipe(self) -> Recipe | None:
        if not self._ingredients:
            return None
        ingredients = tuple(
            Ingredient(
                name=item["name"],
                percentage=item["percentage"],
                cost_per_unit=item["cost_per_unit"],
            )
            for item in self._ingredients
        )
        return Recipe(
            id=self._recipe_id,
            name=self._recipe_name,
            description=self._recipe_description,
            version=self._recipe_version,
            updated_at=self._recipe_updated_at,
            is_draft=self._is_draft,
            ingredients=ingredients,
        )

    def set_target_weight(self, weight: Decimal) -> None:
        if weight != self._target_weight and weight > Decimal("0"):
            self._target_weight = weight
            for item in self._ingredients:
                item["weight"] = (item["percentage"] * self._target_weight) / Decimal("100")
            self.recalculate()

    def set_precision(self, precision: Decimal) -> None:
        if precision != self._precision and precision > Decimal("0"):
            self._precision = precision
            self.recalculate()

    def add_ingredient(
        self,
        name: str = "New Ingredient",
        percentage: Decimal = Decimal("0.00"),
        cost_per_unit: Decimal = Decimal("0.00"),
        weight: Decimal | None = None,
    ) -> None:
        row_pos = len(self._ingredients)
        next_order = self._next_original_order
        self._next_original_order += 1
        if weight is None:
            weight = (
                (percentage * self._target_weight) / Decimal("100")
                if percentage > Decimal("0")
                else Decimal("0.00")
            )
        self.beginInsertRows(QModelIndex(), row_pos, row_pos)
        self._ingredients.append(
            {
                "name": name,
                "percentage": percentage,
                "cost_per_unit": cost_per_unit,
                "weight": weight,
                "original_order": next_order,
            }
        )
        self.endInsertRows()
        self.recalculate()

    def remove_ingredient(self, row: int) -> None:
        if 0 <= row < len(self._ingredients):
            self.beginRemoveRows(QModelIndex(), row, row)
            del self._ingredients[row]
            self.endRemoveRows()
            self.recalculate()

    def _set_invalid_state(self, message: str) -> None:
        self._current_batch = None
        self._is_valid = False
        self._cost_precision = 2
        self.validation_changed.emit(False, message)
        self.batch_updated.emit(None)
        self._notify_derived_data_changed()

    def recalculate(self) -> None:
        with decimal.localcontext(CALCULATION_CONTEXT):
            total_pct = sum((item["percentage"] for item in self._ingredients), Decimal("0"))

        if not self._ingredients:
            self._set_invalid_state("Formula is empty. Add ingredients to begin.")
            return

        if any(not item["name"].strip() for item in self._ingredients):
            self._set_invalid_state("Formula contains ingredients with empty names")
            return

        seen_names = set()
        for item in self._ingredients:
            k = item["name"].strip().lower()
            if k in seen_names:
                self._set_invalid_state(f"Formula contains duplicate ingredient: '{item['name'].strip()}'")
                return
            seen_names.add(k)

        if any(item["percentage"] <= Decimal("0") for item in self._ingredients):
            self._set_invalid_state(f"Formula Total: {total_pct:.2f}% (Ingredients must be > 0.00%)")
            return

        try:
            recipe = self.get_recipe()
            batch = compute_batch(recipe, target_weight=self._target_weight, precision=self._precision)
            self._current_batch = batch
            self._is_valid = True
            self._cost_precision = determine_cost_precision([item.cost for item in batch.items])

            if abs(total_pct - Decimal("100.00")) <= Decimal("1e-9") or total_pct == Decimal("100"):
                status_msg = "Formula Total: 100.00% (Valid)"
            else:
                status_msg = f"Formula Total: {total_pct:.4f}% (Valid within tolerance)"
            if batch.zero_weight_ingredients:
                status_msg += f" ⚠ {len(batch.zero_weight_ingredients)} trace component(s) round to 0.00 mass"
            if batch.high_deviation_ingredients:
                status_msg += (
                    f" ⚠ {len(batch.high_deviation_ingredients)} component(s) shifted >25% due to scale precision"
                )

            self.validation_changed.emit(True, status_msg)
            self.batch_updated.emit(batch)
            self._notify_derived_data_changed()
        except IncompatibleTargetPrecisionError as err:
            self._set_invalid_state(f"Precision Error: {err}")
        except PercentageSumError:
            self._set_invalid_state(f"Formula Total: {total_pct:.2f}% (Must equal 100.00%)")
        except RecipeCalculationError as err:
            self._set_invalid_state(f"Calculation Error: {err}")
        except Exception as err:
            import logging

            logging.getLogger(__name__).exception("Unexpected calculation error in recipe table model: %s", err)
            self._set_invalid_state(f"Calculation Error: {err}")

    def _notify_derived_data_changed(self) -> None:
        if self._ingredients:
            top_left = self.index(0, 3)
            bottom_right = self.index(len(self._ingredients) - 1, 5)
            self.dataChanged.emit(top_left, bottom_right, [Qt.ItemDataRole.DisplayRole])
