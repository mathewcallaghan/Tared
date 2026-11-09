"""Primary application window with sidebar archive, search, and reactive formulation table."""

import os
import re
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QModelIndex, QPoint, Qt
from PyQt6.QtGui import QCloseEvent, QIcon
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QCompleter,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from tared.config import ConfigManager
from tared.core.formatting import format_currency
from tared.core.models import CatalogIngredient, Ingredient, Recipe
from tared.core.service import RecipeService
from tared.storage.base import RecipeRepository
from tared.storage.json_repo import load_recipe_from_file, save_recipe_to_file
from tared.storage.workspace_io import export_workspace_to_json_file
from tared.ui.models.recipe_table_model import RecipeTableModel
from tared.ui.views.about_dialog import AboutDialog
from tared.ui.views.batch_history_dialog import BatchHistoryDialog
from tared.ui.views.catalog_dialog import CatalogManagerDialog
from tared.ui.views.catalog_picker_dialog import CatalogPickerDialog
from tared.ui.views.data_transfer_dialog import DataTransferDialog
from tared.ui.views.print_dialog import PrintBatchSheetDialog
from tared.ui.views.settings_dialog import SettingsDialog
from tared.ui.views.update_dialog import UpdateDialog


class IngredientNameDelegate(QStyledItemDelegate):
    """Provides a QCompleter for ingredient name cells tied to the catalog."""

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


def load_app_icon() -> QIcon:
    """Load application window icon using importlib.resources with frozen AppDir fallback."""
    for name in ("tared-icon.svg", "tared.png"):
        try:
            import importlib.resources

            ref = importlib.resources.files("tared").joinpath("assets", name)
            if ref.is_file():
                with importlib.resources.as_file(ref) as asset_path:
                    icon = QIcon(str(asset_path))
                    if not icon.isNull():
                        return icon
        except Exception:
            pass
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base_dir = Path(sys._MEIPASS)
        candidate_paths = (
            base_dir / "tared" / "assets" / "tared-icon.svg",
            base_dir / "tared" / "assets" / "tared.png",
            base_dir / "assets" / "tared-icon.svg",
            base_dir / "tared.png",
        )
        for target in candidate_paths:
            if target.is_file():
                icon = QIcon(str(target))
                if not icon.isNull():
                    return icon

    icon = QIcon.fromTheme("tared")
    if not icon.isNull():
        return icon

    return QIcon()


class MainWindow(QMainWindow):
    """Main window orchestrating recipe archive navigation, editing, and batch sheet printing."""

    def __init__(self, repository: RecipeRepository, config: ConfigManager | None = None) -> None:
        super().__init__()
        self.repo = repository
        self.service = RecipeService(repository)
        self.config = config if config is not None else ConfigManager()
        self._is_dirty: bool = False
        self._is_switching_recipe: bool = False
        self._current_recipe_id: str | None = None
        self.setWindowTitle("Tared - Formulation & Batch Management")
        try:
            icon = load_app_icon()
            if not icon.isNull():
                self.setWindowIcon(icon)
        except Exception:
            pass
        self.resize(1020, 680)
        self.setMinimumWidth(760)
        self.setStatusBar(QStatusBar(self))

        self._setup_ui()
        self._setup_menu_bar()
        self._load_recipe_archive()
        self._init_startup_checks()

    def _init_startup_checks(self) -> None:
        if (
            self.config.check_updates_on_startup
            and not os.environ.get("TARED_DISABLE_UPDATE_CHECK")
            and os.environ.get("QT_QPA_PLATFORM") != "offscreen"
        ):
            from PyQt6.QtCore import QTimer

            QTimer.singleShot(1500, self._check_updates_silently)

    def _check_updates_silently(self) -> None:
        import tared
        from tared.ui.models.update_worker import CheckUpdateWorker

        curr_ver = getattr(tared, "__version__", "1.0.0")
        self._startup_update_worker = CheckUpdateWorker(curr_ver, self)
        self._startup_update_worker.check_finished.connect(self._on_startup_update_checked)
        self._startup_update_worker.start()

    def _on_startup_update_checked(self, is_available: bool, info: Any, msg: str) -> None:
        if is_available and info is not None:
            self.statusBar().showMessage(
                f"Update available: {info.tag_name}. Use Help -> Check for Updates to install.",
                12000,
            )

    def _setup_ui(self) -> None:
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter)

        # 1. Left Sidebar: Recipe Archive Navigation & Search
        sidebar = QWidget()
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(4, 4, 4, 4)

        archive_label = QLabel("Formula Archive")
        archive_label.setStyleSheet("font-weight: bold; font-size: 13pt; margin-bottom: 2px;")
        side_layout.addWidget(archive_label)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search formulas...")
        self.search_input.setClearButtonEnabled(True)
        side_layout.addWidget(self.search_input)
        self.recipe_list = QListWidget()
        self.recipe_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.recipe_list.customContextMenuRequested.connect(self._on_recipe_list_context_menu)
        side_layout.addWidget(self.recipe_list)

        side_btn_layout = QHBoxLayout()
        self.btn_new = QPushButton("+ New")
        self.btn_delete = QPushButton("Archive")
        side_btn_layout.addWidget(self.btn_new)
        side_btn_layout.addWidget(self.btn_delete)
        side_layout.addLayout(side_btn_layout)

        # Backward-compatible references
        self.btn_import = QPushButton("Import JSON")
        self.btn_export = QPushButton("Export JSON")
        self.btn_catalog = QPushButton("Raw Materials Catalog...")
        self.btn_import.clicked.connect(self._import_recipe)
        self.btn_export.clicked.connect(self._export_recipe)
        self.btn_catalog.clicked.connect(self._open_catalog_dialog)

        sidebar.setMaximumWidth(280)
        splitter.addWidget(sidebar)

        # 2. Right Editor Panel
        editor_widget = QWidget()
        editor_layout = QVBoxLayout(editor_widget)

        # Header Metadata Row
        header_form = QFormLayout()
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Formula Name")
        self.desc_input = QLineEdit()
        self.desc_input.setPlaceholderText("Description / Compounding SOP")
        self.rev_label = QLabel("Rev: v1")
        self.rev_label.setStyleSheet("color: #666; font-size: 9.5pt;")

        header_form.addRow("Formula Name:", self.name_input)
        header_form.addRow("Description:", self.desc_input)
        header_form.addRow("Version Status:", self.rev_label)
        editor_layout.addLayout(header_form)

        # Scaling, Batch Control, and Precision Selector
        batch_ctrl_layout = QHBoxLayout()
        batch_ctrl_layout.addWidget(QLabel("Target Batch Mass (g):"))

        self.target_weight_spin = QDoubleSpinBox()
        self.target_weight_spin.setRange(0.01, 10_000_000.0)
        self.target_weight_spin.setValue(1000.00)
        self.target_weight_spin.setDecimals(2)
        self.target_weight_spin.setSingleStep(100.0)
        batch_ctrl_layout.addWidget(self.target_weight_spin)

        batch_ctrl_layout.addSpacing(15)
        batch_ctrl_layout.addWidget(QLabel("Scale Precision:"))
        self.precision_combo = QComboBox()
        self.precision_combo.addItem("0.001 g", "0.001")
        self.precision_combo.addItem("0.01 g", "0.01")
        self.precision_combo.addItem("0.1 g", "0.1")
        self.precision_combo.addItem("1 g", "1")
        init_prec_idx = self.precision_combo.findData(self.config.default_precision)
        self.precision_combo.setCurrentIndex(init_prec_idx if init_prec_idx >= 0 else 1)
        batch_ctrl_layout.addWidget(self.precision_combo)

        batch_ctrl_layout.addSpacing(20)
        self.validation_label = QLabel("Total: 0.00%")
        self.validation_label.setStyleSheet("font-weight: bold; padding: 4px 8px; border-radius: 4px;")
        batch_ctrl_layout.addWidget(self.validation_label)
        batch_ctrl_layout.addStretch()

        editor_layout.addLayout(batch_ctrl_layout)

        # Ingredient Table
        self.table_model = RecipeTableModel(self)
        self.table_model.set_catalog_lookup(self.service.get_catalog_price)
        self.table_view = QTableView()
        self.table_view.setModel(self.table_model)

        self.table_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table_view.customContextMenuRequested.connect(self._on_table_context_menu)

        self.table_view.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table_view.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table_view.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table_view.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table_view.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table_view.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.table_view.setItemDelegateForColumn(0, IngredientNameDelegate(self._get_catalog_names, self.table_view))
        editor_layout.addWidget(self.table_view)

        # Table Control Bar: Row Operations (Left) and Sequence / Catalog Tools (Right)
        tbl_btn_layout = QHBoxLayout()
        tbl_btn_layout.setContentsMargins(0, 4, 0, 4)

        self.btn_add_row = QPushButton("+ Add Ingredient")
        self.btn_add_row.setToolTip("Add a new ingredient row")
        self.btn_add_from_catalog = QPushButton("+ From Catalog...")
        self.btn_add_from_catalog.setToolTip("Select ingredients from the raw materials catalog")
        self.btn_remove_row = QPushButton("− Remove")
        self.btn_remove_row.setToolTip("Remove selected ingredient row(s)")
        self.btn_move_up = QPushButton("▲")
        self.btn_move_up.setFixedWidth(32)
        self.btn_move_up.setToolTip("Move selected row up in compounding order")
        self.btn_move_down = QPushButton("▼")
        self.btn_move_down.setFixedWidth(32)
        self.btn_move_down.setToolTip("Move selected row down in compounding order")

        self.btn_catalog_menu = QPushButton("Catalog ▾")
        self.btn_catalog_menu.setToolTip("Master catalog price lookup and synchronization")
        catalog_menu = QMenu(self)
        self.action_add_to_catalog = catalog_menu.addAction("Add Selected to Catalog")
        self.action_add_to_catalog.setToolTip("Add or update the selected ingredient(s) in the master catalog.")
        self.action_sync_costs = catalog_menu.addAction("Sync Costs from Catalog")
        self.action_sync_costs.setToolTip("Update ingredient unit costs with current prices from the master catalog.")
        catalog_menu.addSeparator()
        self.action_open_catalog = catalog_menu.addAction("Open Catalog Manager...")
        self.action_open_catalog.setToolTip("View and edit the complete raw materials catalog.")
        self.btn_catalog_menu.setMenu(catalog_menu)

        # Retain backward-compatible references
        self.btn_add_to_catalog = self.action_add_to_catalog
        self.btn_sync_costs = self.action_sync_costs

        tbl_btn_layout.addWidget(self.btn_add_row)
        tbl_btn_layout.addWidget(self.btn_add_from_catalog)
        tbl_btn_layout.addWidget(self.btn_remove_row)
        tbl_btn_layout.addWidget(self.btn_move_up)
        tbl_btn_layout.addWidget(self.btn_move_down)
        tbl_btn_layout.addSpacing(6)
        tbl_btn_layout.addWidget(self.btn_catalog_menu)
        tbl_btn_layout.addStretch()

        seq_label = QLabel("Sequence:")
        seq_label.setToolTip("Compounding sequence (reorders formula rows)")
        tbl_btn_layout.addWidget(seq_label)

        self.sort_combo = QComboBox()
        self.sort_combo.addItem("Manual / As Entered", "manual")
        self.sort_combo.addItem("Percentage (High → Low)", "pct_desc")
        self.sort_combo.addItem("Percentage (Low → High)", "pct_asc")
        self.sort_combo.addItem("Name (A → Z)", "name_asc")
        self.sort_combo.setToolTip("Select sorting rule (modifies formula order)")
        tbl_btn_layout.addWidget(self.sort_combo)

        editor_layout.addLayout(tbl_btn_layout)

        # Bottom Bar: Summary & Actions
        bottom_bar = QFrame()
        bottom_bar.setFrameShape(QFrame.Shape.StyledPanel)
        bottom_layout = QHBoxLayout(bottom_bar)

        self.summary_label = QLabel("Batch Total: 0.00 g | Cost: $0.00")
        self.summary_label.setStyleSheet("font-weight: bold; font-size: 11pt;")
        bottom_layout.addWidget(self.summary_label)
        bottom_layout.addStretch()

        self.btn_save = QPushButton("Save Formula")
        self.btn_save.setStyleSheet("font-weight: bold; padding: 6px 14px;")
        self.btn_print = QPushButton("Print Batch Sheet...")
        self.btn_print.setStyleSheet("padding: 6px 14px;")
        self.btn_history = QPushButton("Batch History...")
        self.btn_history.setStyleSheet("padding: 6px 14px;")
        bottom_layout.addWidget(self.btn_save)
        bottom_layout.addWidget(self.btn_print)
        bottom_layout.addWidget(self.btn_history)

        editor_layout.addWidget(bottom_bar)
        splitter.addWidget(editor_widget)
        splitter.setStretchFactor(1, 4)

        # Connections
        self.search_input.textChanged.connect(self._filter_recipes)
        self.recipe_list.currentRowChanged.connect(self._on_recipe_selected)
        self.btn_new.clicked.connect(self._create_new_recipe)
        self.btn_delete.clicked.connect(self._delete_current_recipe)
        self.btn_save.clicked.connect(lambda: self._save_current_recipe())
        self.btn_import.clicked.connect(self._import_recipe)
        self.btn_export.clicked.connect(self._export_recipe)
        self.btn_catalog.clicked.connect(self._open_catalog_dialog)
        self.action_add_to_catalog.triggered.connect(self._add_selected_to_catalog)
        self.action_sync_costs.triggered.connect(self._sync_costs_with_catalog)
        self.action_open_catalog.triggered.connect(self._open_catalog_dialog)
        self.btn_add_row.clicked.connect(lambda: self.table_model.add_ingredient(name=""))
        self.btn_add_from_catalog.clicked.connect(self._add_from_catalog)
        self.btn_remove_row.clicked.connect(self._remove_selected_ingredient)
        self.btn_move_up.clicked.connect(self._move_selected_row_up)
        self.btn_move_down.clicked.connect(self._move_selected_row_down)
        self.sort_combo.currentIndexChanged.connect(self._apply_sort_order)
        self.btn_print.clicked.connect(self._open_print_dialog)
        self.btn_history.clicked.connect(self._open_batch_history_dialog)

        self.target_weight_spin.valueChanged.connect(self._on_target_weight_changed)
        self.precision_combo.currentIndexChanged.connect(self._on_precision_changed)
        self.table_model.validation_changed.connect(self._on_validation_changed)
        self.table_model.batch_updated.connect(self._on_batch_updated)

        self.name_input.textChanged.connect(self._on_header_metadata_changed)
        self.desc_input.textChanged.connect(self._on_header_metadata_changed)
        self.table_model.dataChanged.connect(self._on_table_data_changed)
        self.table_model.rowsInserted.connect(lambda: self._set_dirty(True))
        self.table_model.rowsRemoved.connect(lambda: self._set_dirty(True))
        self.table_model.edit_rejected.connect(lambda msg: self.statusBar().showMessage(msg, 4000))

    def _on_header_metadata_changed(self) -> None:
        if not self._is_switching_recipe:
            self._set_dirty(True)
            self.table_model.set_live_metadata(
                name=self.name_input.text().strip(),
                description=self.desc_input.text().strip(),
            )

    def _on_table_data_changed(self, top_left: QModelIndex, bottom_right: QModelIndex, roles: list[Any] = []) -> None:
        if top_left.column() <= 2:
            self._set_dirty(True)

    def _set_dirty(self, dirty: bool = True) -> None:
        if self._is_switching_recipe and dirty:
            return
        self._is_dirty = dirty
        base_title = "Tared - Formulation & Batch Management"
        if self._is_dirty:
            self.setWindowTitle(f"{base_title} [Unsaved Changes *]")
        else:
            self.setWindowTitle(base_title)

    def _filter_recipes(self, text: str) -> None:
        query = text.strip().lower()
        for idx in range(self.recipe_list.count()):
            item = self.recipe_list.item(idx)
            item.setHidden(query not in item.text().lower())

    def _on_precision_changed(self) -> None:
        val_str = self.precision_combo.currentData()
        precision = Decimal(val_str)
        exp = precision.as_tuple().exponent
        decimals = max(0, -exp) if isinstance(exp, int) else 0
        self.target_weight_spin.setDecimals(decimals)
        self.target_weight_spin.setSingleStep(float(precision))
        self.target_weight_spin.setMinimum(float(precision))
        self.table_model.set_precision(precision)

    def _move_selected_row_up(self) -> None:
        current_idx = self.table_view.currentIndex()
        if current_idx.isValid() and current_idx.row() > 0:
            target_row = current_idx.row()
            if self.table_model.move_ingredient_up(target_row):
                new_idx = self.table_model.index(target_row - 1, current_idx.column())
                self.table_view.setCurrentIndex(new_idx)
                self.sort_combo.setCurrentIndex(0)
                self._set_dirty(True)

    def _move_selected_row_down(self) -> None:
        current_idx = self.table_view.currentIndex()
        if current_idx.isValid() and current_idx.row() < self.table_model.rowCount() - 1:
            target_row = current_idx.row()
            if self.table_model.move_ingredient_down(target_row):
                new_idx = self.table_model.index(target_row + 1, current_idx.column())
                self.table_view.setCurrentIndex(new_idx)
                self.sort_combo.setCurrentIndex(0)
                self._set_dirty(True)

    def _apply_sort_order(self) -> None:
        mode = self.sort_combo.currentData()
        changed = False
        if mode == "pct_desc":
            changed = self.table_model.sort_by_percentage(descending=True)
        elif mode == "pct_asc":
            changed = self.table_model.sort_by_percentage(descending=False)
        elif mode == "name_asc":
            changed = self.table_model.sort_by_name(ascending=True)
        elif mode == "manual":
            changed = self.table_model.restore_original_order()

        if changed and not self._is_switching_recipe:
            self._set_dirty(True)

    def _load_recipe_archive(self, select_id: str | None = None) -> None:
        prev_switching = self._is_switching_recipe
        self._is_switching_recipe = True
        try:
            self.recipe_list.clear()
            recipes = self.repo.list_recipes()
            selected_row = -1

            for idx, r in enumerate(recipes):
                display_title = f"{r.name} (v{r.version})"
                item = QListWidgetItem(display_title)
                item.setData(Qt.ItemDataRole.UserRole, r.id)
                self.recipe_list.addItem(item)
                if select_id is not None and r.id == select_id:
                    selected_row = idx

            if recipes:
                target_idx = selected_row if selected_row >= 0 else 0
                self.recipe_list.setCurrentRow(target_idx)
                target_item = self.recipe_list.item(target_idx)
                if target_item is not None:
                    target_id = target_item.data(Qt.ItemDataRole.UserRole)
                    self._load_selected_recipe(target_id)
            else:
                self._create_new_recipe()

            if self.search_input.text().strip():
                self._filter_recipes(self.search_input.text())
        finally:
            self._is_switching_recipe = prev_switching

    def _on_recipe_selected(self, row: int) -> None:
        if self._is_switching_recipe or row < 0:
            return

        item = self.recipe_list.item(row)
        if item is None:
            return

        new_recipe_id = item.data(Qt.ItemDataRole.UserRole)
        if new_recipe_id == self._current_recipe_id:
            return

        if self._is_dirty:
            reply = QMessageBox.question(
                self,
                "Unsaved Changes",
                "You have unsaved changes in the active formula.\nDo you want to save before navigating away?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if reply == QMessageBox.StandardButton.Save:
                if not self._save_current_recipe(target_select_id=new_recipe_id):
                    self._revert_selection(self._current_recipe_id)
                    return
                return
            elif reply == QMessageBox.StandardButton.Cancel:
                self._revert_selection(self._current_recipe_id)
                return
            else:
                self._set_dirty(False)

        self._load_selected_recipe(new_recipe_id)

    def _revert_selection(self, recipe_id: str | None) -> None:
        prev_switching = self._is_switching_recipe
        self._is_switching_recipe = True
        try:
            if recipe_id is None:
                self.recipe_list.clearSelection()
                self.recipe_list.setCurrentRow(-1)
                return
            for idx in range(self.recipe_list.count()):
                if self.recipe_list.item(idx).data(Qt.ItemDataRole.UserRole) == recipe_id:
                    self.recipe_list.setCurrentRow(idx)
                    break
        finally:
            self._is_switching_recipe = prev_switching

    def _load_selected_recipe(self, recipe_id: str) -> None:
        recipe = self.repo.get_recipe(recipe_id)
        if recipe is not None:
            prev_switching = self._is_switching_recipe
            self._is_switching_recipe = True
            try:
                self._current_recipe_id = recipe.id
                self.name_input.setText(recipe.name)
                self.desc_input.setText(recipe.description)
                updated_text = f" (Updated: {recipe.updated_at})" if recipe.updated_at else ""
                self.rev_label.setText(f"Revision: v{recipe.version}{updated_text}")

                self.sort_combo.setCurrentIndex(0)
                self.table_model.load_recipe(recipe, auto_sort=False)
                self._set_dirty(False)
            finally:
                self._is_switching_recipe = prev_switching

    def _create_new_recipe(self) -> None:
        if self._is_dirty:
            reply = QMessageBox.question(
                self,
                "Unsaved Changes",
                "Discard unsaved changes and create a new formula?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        prev_switching = self._is_switching_recipe
        self._is_switching_recipe = True
        try:
            self.recipe_list.clearSelection()
            self.recipe_list.setCurrentRow(-1)
            self._current_recipe_id = None
            self.name_input.setText("New Formulation")
            self.desc_input.setText("")
            self.rev_label.setText("Revision: v1 (Unsaved Draft)")
            self.sort_combo.setCurrentIndex(0)
            self.table_model.new_draft(name="New Formulation", description="")
            self._set_dirty(False)
        finally:
            self._is_switching_recipe = prev_switching

    def _validate_formulation_before_action(self, action_name: str = "save") -> bool:
        name = self.name_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Validation Error", "Formula name cannot be empty.")
            return False

        if self.table_model.rowCount() == 0:
            QMessageBox.warning(self, "Validation Error", "Formula must contain at least one ingredient.")
            return False

        for row, item in enumerate(self.table_model.ingredients_data):
            if not item["name"].strip():
                QMessageBox.warning(
                    self,
                    "Validation Error",
                    f"Row {row + 1} has an empty ingredient name.",
                )
                return False
            if item["percentage"] <= Decimal("0"):
                QMessageBox.warning(
                    self,
                    "Validation Error",
                    f"Ingredient '{item['name']}' in row {row + 1} has percentage {item['percentage']}%. "
                    f"All ingredients must have positive percentages (> 0.00%) before you can {action_name}.",
                )
                return False

        total_pct = sum((item["percentage"] for item in self.table_model.ingredients_data), Decimal("0"))
        if abs(total_pct - Decimal("100.00")) > Decimal("0.0001"):
            if action_name == "save":
                msg_lines = [
                    f"Formula percentages sum to {total_pct:.2f}% (not 100.00%).",
                    "",
                    "Batch sheet generation and production execution remain locked until the formula sums to 100.00%.",
                    "",
                    "Do you want to save this formulation as an in-progress draft?",
                ]
                reply = QMessageBox.question(
                    self,
                    "Save Incomplete Formula Draft?",
                    "\n".join(msg_lines),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return False
            else:
                QMessageBox.warning(
                    self,
                    "Validation Error",
                    f"Formula percentages must sum to 100.00% before you can {action_name} "
                    f"(currently {total_pct:.2f}%).",
                )
                return False

        return True

    def _get_active_recipe_model(self) -> Recipe | None:
        if self.table_model.rowCount() == 0:
            return None

        name = self.name_input.text().strip() or "Untitled Recipe"
        desc = self.desc_input.text().strip()

        ingredients = tuple(
            Ingredient(
                name=item["name"],
                percentage=item["percentage"],
                cost_per_unit=item["cost_per_unit"],
            )
            for item in self.table_model.ingredients_data
        )

        return Recipe(
            id=self.table_model.recipe_id or str(uuid.uuid4()),
            name=name,
            description=desc,
            version=self.table_model.recipe_version,
            updated_at=self.table_model.recipe_updated_at,
            ingredients=ingredients,
        )

    def _save_current_recipe(self, target_select_id: str | None = None) -> bool:
        if not isinstance(target_select_id, str):
            target_select_id = None

        if not self._validate_formulation_before_action(action_name="save"):
            return False

        active_recipe = self._get_active_recipe_model()
        existing = self.repo.get_recipe(active_recipe.id, include_archived=True)
        is_archived = existing is not None and self.repo.get_recipe(active_recipe.id, include_archived=False) is None
        if is_archived:
            reply = QMessageBox.question(
                self,
                "Restore Archived Formula",
                (
                    f"Formula '{active_recipe.name}' is currently archived.\n\n"
                    "Saving will restore it to the active formula library. Proceed?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return False

        if not self._is_dirty and existing is not None and target_select_id is None and not is_archived:
            self.statusBar().showMessage("No changes detected; formula is up to date.", 2500)
            return True

        try:
            saved_recipe = self.service.save_recipe(active_recipe)
        except Exception as err:
            QMessageBox.critical(self, "Database Save Error", f"Failed to save recipe:\n{err}")
            return False

        self._set_dirty(False)
        select_id = target_select_id or saved_recipe.id
        self._load_recipe_archive(select_id=select_id)

        self.statusBar().showMessage(
            f"Formula '{saved_recipe.name}' (v{saved_recipe.version}) saved successfully.", 3500
        )
        return True

    def _delete_current_recipe(self) -> None:
        if self._current_recipe_id is None or self.repo.get_recipe(self._current_recipe_id) is None:
            if self._is_dirty:
                reply = QMessageBox.question(
                    self,
                    "Discard Draft",
                    "Discard this unsaved new recipe draft?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    self._set_dirty(False)
                    self._current_recipe_id = None
                    self._load_recipe_archive()
            return

        if self._is_dirty:
            reply = QMessageBox.question(
                self,
                "Unsaved Changes",
                "You have unsaved changes in the active recipe.\nDo you want to save before archiving?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if reply == QMessageBox.StandardButton.Save:
                if not self._save_current_recipe():
                    return
            elif reply == QMessageBox.StandardButton.Cancel:
                return

        if self._current_recipe_id is None:
            return

        recipe = self.repo.get_recipe(self._current_recipe_id)
        if recipe is not None:
            reply = QMessageBox.question(
                self,
                "Confirm Archive",
                f"Archive formula '{recipe.name}' and remove it from active library?\n"
                "(Historical batch records and revisions remain preserved)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                try:
                    self.repo.delete_recipe(recipe.id)
                except Exception as err:
                    QMessageBox.critical(self, "Archive Error", f"Failed to archive recipe:\n{err}")
                    return
                self._set_dirty(False)
                self._current_recipe_id = None
                self._load_recipe_archive()

    def _remove_selected_ingredient(self) -> None:
        selection_model = self.table_view.selectionModel()
        selected_rows = [idx.row() for idx in selection_model.selectedRows()]

        if not selected_rows:
            current_idx = self.table_view.currentIndex()
            if current_idx.isValid():
                selected_rows = [current_idx.row()]

        if not selected_rows:
            return

        for row in sorted(set(selected_rows), reverse=True):
            self.table_model.remove_ingredient(row)
        self._set_dirty(True)

    def _on_target_weight_changed(self, val: float) -> None:
        try:
            # Read cleanly parsed string from the spinbox instead of the float value
            # to avoid IEEE 754 precision loss and localized comma issues
            raw_text = self.target_weight_spin.cleanText()
            clean_str = raw_text.replace(",", ".") if "." not in raw_text else raw_text.replace(",", "")
            self.table_model.set_target_weight(Decimal(clean_str))
        except (InvalidOperation, ValueError):
            pass

    def _on_validation_changed(self, is_valid: bool, msg: str) -> None:
        self.validation_label.setText(msg)
        if is_valid:
            self.validation_label.setStyleSheet(
                "color: #0f5132; background-color: #d1e7dd; font-weight: bold; padding: 4px 8px; border-radius: 4px;"
            )
            self.btn_print.setEnabled(True)
        elif self.table_model.rowCount() == 0:
            self.validation_label.setStyleSheet(
                "color: #475569; background-color: #f1f5f9; font-weight: normal; padding: 4px 8px; border-radius: 4px;"
            )
            self.btn_print.setEnabled(False)
        else:
            self.validation_label.setStyleSheet(
                "color: #842029; background-color: #f8d7da; font-weight: bold; padding: 4px 8px; border-radius: 4px;"
            )
            self.btn_print.setEnabled(False)

    def _on_batch_updated(self, batch: Any) -> None:
        if batch is not None:
            decimals = self.table_model.precision_decimals
            summary_parts = [
                f"Batch Target: {batch.target_weight:.{decimals}f} g",
                f"Total: {batch.total_weight:.{decimals}f} g",
            ]
            if batch.max_allocation_deviation > Decimal("0"):
                summary_parts.append(f"Max Shift: at most {batch.max_allocation_deviation:.{decimals}f} g")
            summary_parts.append(
                f"Cost: {format_currency(batch.total_cost, precision=self.table_model.cost_precision)}"
            )
            self.summary_label.setText(" | ".join(summary_parts))
        else:
            self.summary_label.setText("Batch Total: — | Cost: —")

    def _on_table_context_menu(self, pos: QPoint) -> None:
        selected_rows = self._get_selected_table_rows()
        if not selected_rows:
            index = self.table_view.indexAt(pos)
            if index.isValid():
                selected_rows = [index.row()]

        if not selected_rows:
            return

        menu = QMenu(self)
        if len(selected_rows) == 1:
            row = selected_rows[0]
            name = str(self.table_model.data(self.table_model.index(row, 0)) or "Selected")
            unit_cost = format_currency(self.table_model.ingredients_data[row]["cost_per_unit"])
            action_text = f"Add '{name}' ({unit_cost}/kg) to Raw Materials Catalog"
        else:
            action_text = f"Add {len(selected_rows)} Selected Ingredients to Raw Materials Catalog"

        add_action = menu.addAction(action_text)
        add_action.triggered.connect(self._add_selected_to_catalog)
        menu.exec(self.table_view.viewport().mapToGlobal(pos))

    def _get_selected_table_rows(self) -> list[int]:
        selected_indexes = self.table_view.selectionModel().selectedRows()
        if selected_indexes:
            return sorted(set(idx.row() for idx in selected_indexes))
        current = self.table_view.currentIndex()
        if current.isValid():
            return [current.row()]
        return []

    def _get_catalog_names(self) -> list[str]:
        return [item.name for item in self.repo.list_catalog_ingredients()]

    def _add_from_catalog(self) -> None:
        dialog = CatalogPickerDialog(self.repo, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_items:
            for item in dialog.selected_items:
                self.table_model.add_ingredient(
                    name=item.name,
                    percentage=Decimal("0.00"),
                    cost_per_unit=item.cost_per_unit,
                )
            self._set_dirty(True)

    def _add_selected_to_catalog(self) -> None:
        rows = self._get_selected_table_rows()
        if not rows:
            self.statusBar().showMessage("Select an ingredient row first.", 3000)
            return

        added_count = 0
        updated_count = 0

        for row in rows:
            if row >= len(self.table_model.ingredients_data):
                continue
            item = self.table_model.ingredients_data[row]
            name = item["name"].strip()
            if not name:
                continue
            cost = item["cost_per_unit"]

            status, existing_cost = self.service.save_ingredient_to_catalog(name, cost)
            if status == "added":
                added_count += 1
            elif status == "identical":
                continue
            elif status == "different" and existing_cost is not None:
                reply = QMessageBox.question(
                    self,
                    "Update Master Catalog Price?",
                    f"'{name}' is already in the master catalog at {format_currency(existing_cost)}/kg.\n\n"
                    f"Update catalog default price to {format_currency(cost)}/kg?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    self.repo.save_catalog_ingredient(
                        CatalogIngredient(
                            name=name,
                            cost_per_unit=cost,
                            updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        )
                    )
                    updated_count += 1

        total_actions = added_count + updated_count
        if total_actions > 0:
            msg = f"Catalog updated: {added_count} new raw material(s) added, {updated_count} price(s) updated."
            self.statusBar().showMessage(msg, 3500)
        else:
            self.statusBar().showMessage(
                "Selected ingredient(s) are already present in catalog with matching prices.", 3000
            )

    def _open_catalog_dialog(self) -> None:
        active_recipe = self._get_active_recipe_model()
        dialog = CatalogManagerDialog(self.repo, self.service, current_recipe=active_recipe, parent=self)
        dialog.exec()

    def _sync_costs_with_catalog(self) -> None:
        active_recipe = self._get_active_recipe_model()
        if not active_recipe or not active_recipe.ingredients:
            self.statusBar().showMessage("Formula has no ingredients to sync with catalog.", 3000)
            return
        _, changes = self.service.sync_recipe_costs_from_catalog(active_recipe)

        if not changes:
            self.statusBar().showMessage("All ingredient costs match the master catalog.", 3000)
            return

        change_lines = [
            f"• {name}: {format_currency(old_c)} → {format_currency(new_c)}/kg" for name, old_c, new_c in changes
        ]
        prompt_text = (
            f"The following {len(changes)} ingredient price updates were found in the catalog:\n\n"
            + "\n".join(change_lines)
            + "\n\nUpdate formulation with catalog prices?"
        )

        reply = QMessageBox.question(
            self,
            "Sync Unit Costs from Catalog",
            prompt_text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Yes:
            # Apply to table model
            for row in range(self.table_model.rowCount()):
                ing_name = self.table_model.data(self.table_model.index(row, 0))
                cat_price = self.service.get_catalog_price(ing_name)
                if cat_price is not None:
                    self.table_model.setData(self.table_model.index(row, 2), str(cat_price))
            self._set_dirty(True)
            self.statusBar().showMessage(f"Updated {len(changes)} ingredient cost(s) from catalog.", 3500)

    def _open_batch_history_dialog(self) -> None:
        if not self._current_recipe_id or self.repo.get_recipe(self._current_recipe_id) is None:
            QMessageBox.information(
                self,
                "Batch History",
                (
                    "The active formula is an unsaved draft and has no production history.\n\n"
                    "Save the formula and print a batch sheet to begin recording production runs."
                ),
            )
            return

        active_recipe = self.repo.get_recipe(self._current_recipe_id)
        recipe_name = active_recipe.name if active_recipe else "Recipe"
        dialog = BatchHistoryDialog(self._current_recipe_id, recipe_name, self.repo, self)
        dialog.exec()

    def _open_print_dialog(self) -> None:
        # Check if active formulation is an unpersisted draft
        active_id = self.table_model.recipe_id
        is_persisted = bool(active_id and self.repo.get_recipe(active_id) is not None)

        if self._is_dirty or not is_persisted:
            prompt = (
                "The active formula contains unsaved modifications.\n"
                "Save changes before generating a production batch sheet?"
                if self._is_dirty
                else "This formulation draft must be saved to the database prior to issuing a production batch sheet.\n"
                "Save now?"
            )
            reply = QMessageBox.question(
                self,
                "Save Before Printing",
                prompt,
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if reply == QMessageBox.StandardButton.Save:
                if not self._save_current_recipe():
                    return
            else:
                return

        if self.table_model.current_batch is None:
            QMessageBox.warning(self, "Invalid Batch", "Formula must sum to 100.00% to generate a batch sheet.")
            return
        dialog = PrintBatchSheetDialog(self.table_model.current_batch, self, repository=self.repo, config=self.config)
        dialog.exec()

    def _prompt_import_collision(self, incoming: Recipe, existing: Recipe) -> str:
        msg = QMessageBox(self)
        msg.setWindowTitle("Formula Collision Detected")
        msg.setText(
            f"A formula with ID '{incoming.id}' already exists as '{existing.name}' (v{existing.version}).\n\n"
            f"How would you like to handle this import?"
        )
        btn_copy = msg.addButton("Import as New Copy", QMessageBox.ButtonRole.AcceptRole)
        btn_replace = msg.addButton("Overwrite Existing", QMessageBox.ButtonRole.DestructiveRole)
        msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        msg.setDefaultButton(btn_copy)
        msg.exec()

        clicked = msg.clickedButton()
        if clicked == btn_replace:
            return "overwrite"
        elif clicked == btn_copy:
            return "copy"
        return "cancel"

    def _import_recipe(self) -> None:
        if self._is_dirty:
            reply = QMessageBox.question(
                self,
                "Unsaved Changes",
                "You have unsaved changes in the current recipe.\nDo you want to save before importing?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if reply == QMessageBox.StandardButton.Save:
                if not self._save_current_recipe():
                    return
            elif reply == QMessageBox.StandardButton.Cancel:
                return

        file_path, _ = QFileDialog.getOpenFileName(self, "Import Recipe JSON", "", "JSON Files (*.json)")
        if not file_path:
            return

        try:
            recipe = load_recipe_from_file(file_path)
        except Exception as err:
            QMessageBox.critical(self, "Import Error", f"Failed to parse recipe JSON:\n{err}")
            return

        # Check collision against both active and archived recipes
        existing = self.repo.get_recipe(recipe.id, include_archived=True)
        collision_mode = "copy"
        if existing is not None:
            choice = self._prompt_import_collision(recipe, existing)
            if choice == "cancel":
                return
            collision_mode = choice

        try:
            saved_recipe = self.service.import_recipe(recipe, collision_mode=collision_mode)
        except Exception as err:
            QMessageBox.critical(self, "Import Error", f"Failed to persist imported recipe:\n{err}")
            return

        self._set_dirty(False)
        self._load_recipe_archive(select_id=saved_recipe.id)
        self.statusBar().showMessage(
            f"Formula '{saved_recipe.name}' (v{saved_recipe.version}) imported successfully.", 3500
        )

    def _export_recipe(self) -> None:
        if not self._validate_formulation_before_action(action_name="export"):
            return
        active_recipe = self._get_active_recipe_model()
        sanitized_name = re.sub(r'[\/*?:"<>| ]', "_", active_recipe.name)
        start_dir = self.config.get_resolved_export_dir()
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Formula JSON",
            str(start_dir / f"{sanitized_name}_v{active_recipe.version}.json"),
            "JSON Files (*.json)",
        )
        if file_path:
            try:
                save_recipe_to_file(active_recipe, file_path)
                self.statusBar().showMessage(f"Formula exported to {file_path}", 3500)
            except Exception as err:
                QMessageBox.critical(self, "Export Error", f"Failed to export recipe:\n{err}")

    def _setup_menu_bar(self) -> None:
        menubar = self.menuBar()

        # 1. File Menu
        file_menu = menubar.addMenu("&File")

        action_transfer = file_menu.addAction("Data &Transfer && Backup...")
        action_transfer.setShortcut("Ctrl+Shift+E")
        action_transfer.triggered.connect(self._open_data_transfer_dialog)

        file_menu.addSeparator()

        action_settings = file_menu.addAction("&Settings...")
        action_settings.setShortcut("Ctrl+,")
        action_settings.triggered.connect(self._open_settings_dialog)

        file_menu.addSeparator()

        action_exit = file_menu.addAction("E&xit")
        action_exit.setShortcut("Ctrl+Q")
        action_exit.triggered.connect(self.close)

        # 2. Tools Menu
        tools_menu = menubar.addMenu("&Tools")

        action_catalog = tools_menu.addAction("Raw &Materials Catalog...")
        action_catalog.setShortcut("Ctrl+M")
        action_catalog.triggered.connect(self._open_catalog_dialog)

        action_history = tools_menu.addAction("Production &Batch History...")
        action_history.setShortcut("Ctrl+H")
        action_history.triggered.connect(self._open_batch_history_dialog)

        # 3. Help Menu
        help_menu = menubar.addMenu("&Help")
        action_check_updates = help_menu.addAction("Check for &Updates...")
        action_check_updates.triggered.connect(self._check_for_updates)

        action_about = help_menu.addAction("&About Tared")
        action_about.triggered.connect(self._show_about_dialog)

    def _open_data_transfer_dialog(self) -> None:
        dialog = DataTransferDialog(self.repo, self.service, config=self.config, parent=self)
        dialog.workspace_restored.connect(self._on_workspace_restored)
        dialog.recipes_imported.connect(lambda: self._load_recipe_archive())
        dialog.catalog_imported.connect(lambda: self._load_recipe_archive())
        dialog.exec()

    def _open_settings_dialog(self) -> None:
        dialog = SettingsDialog(self.config, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.statusBar().showMessage("Settings updated successfully.", 3000)

    def _on_workspace_restored(self) -> None:
        self._load_recipe_archive()
        self.statusBar().showMessage("Full workspace restored successfully.", 4000)

    def _check_for_updates(self) -> None:
        dialog = UpdateDialog(parent=self, auto_check=True)
        dialog.exec()

    def _show_about_dialog(self) -> None:
        dialog = AboutDialog(parent=self)
        dialog.exec()

    def _on_recipe_list_context_menu(self, pos: QPoint) -> None:
        item = self.recipe_list.itemAt(pos)
        if not item:
            return

        recipe_id = item.data(Qt.ItemDataRole.UserRole)
        recipe = self.repo.get_recipe(recipe_id)
        if not recipe:
            return

        menu = QMenu(self)
        act_export = menu.addAction(f"Export '{recipe.name}' to JSON...")
        act_duplicate = menu.addAction("Duplicate Formula")
        act_history = menu.addAction("View Batch History...")
        menu.addSeparator()
        act_archive = menu.addAction("Archive Formula")

        act_export.triggered.connect(self._export_recipe)
        act_duplicate.triggered.connect(lambda: self._duplicate_selected_recipe(recipe))
        act_history.triggered.connect(self._open_batch_history_dialog)
        act_archive.triggered.connect(self._delete_current_recipe)

        menu.exec(self.recipe_list.viewport().mapToGlobal(pos))

    def _duplicate_selected_recipe(self, recipe: Recipe) -> None:
        dupe = Recipe(
            id=str(uuid.uuid4()),
            name=f"{recipe.name} (Copy)",
            description=recipe.description,
            version=1,
            ingredients=recipe.ingredients,
        )
        saved = self.service.save_recipe(dupe)
        self._load_recipe_archive(select_id=saved.id)
        self.statusBar().showMessage(f"Duplicated formula as '{saved.name}'.", 3500)

    def closeEvent(self, event: QCloseEvent) -> None:
        if getattr(self, "_is_closing", False):
            event.accept()
            return

        def _finalize_exit() -> bool:
            if (
                self.config.is_backup_configured()
                and not getattr(self.repo, "_is_memory", False)
                and hasattr(self.repo, "list_all_recipes_data")
            ):
                try:
                    target_file = Path(self.config.auto_backup_dir) / "tared_workspace.json"
                    export_workspace_to_json_file(self.repo, target_file, max_rotations=3)
                    self.config.record_backup_result(True)
                except Exception as backup_err:
                    import logging

                    logging.getLogger(__name__).warning("Automated workspace backup on exit failed: %s", backup_err)
                    self.config.record_backup_result(False, str(backup_err))
                    reply = QMessageBox.warning(
                        self,
                        "Automated Backup Failed",
                        f"Automated backup to cloud sync folder failed:\n{backup_err}\n\nExit anyway?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    if reply != QMessageBox.StandardButton.Yes:
                        event.ignore()
                        return False
            self._set_dirty(False)
            self._is_closing = True
            self.repo.close()
            event.accept()
            return True

        if self._is_dirty:
            prompt = "You have unsaved changes.\nDo you want to save before exiting?"
            reply = QMessageBox.question(
                self,
                "Unsaved Changes",
                prompt,
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if reply == QMessageBox.StandardButton.Save:
                if self._save_current_recipe():
                    _finalize_exit()
                else:
                    event.ignore()
            elif reply == QMessageBox.StandardButton.Discard:
                _finalize_exit()
            else:
                event.ignore()
        else:
            _finalize_exit()
