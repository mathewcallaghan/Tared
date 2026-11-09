"""Application settings and cloud sync directory preferences dialog."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from tared.config import ConfigManager


class SettingsDialog(QDialog):
    """Modal dialog allowing configuration of backup directories and formulation defaults."""

    def __init__(self, config: ConfigManager, parent: Any = None) -> None:
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Tared Settings")
        self.resize(600, 400)
        self.setMinimumWidth(500)

        self._setup_ui()
        self._load_values()

    def _setup_ui(self) -> None:
        main_layout = QVBoxLayout(self)

        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)

        # Tab 1: Storage & Cloud Sync (literal && prevents mnemonic underscore)
        storage_tab = QWidget()
        storage_layout = QVBoxLayout(storage_tab)
        storage_layout.setSpacing(12)

        info_label = QLabel(
            "Configure automatic backup snapshots and default export locations. "
            "Point these to your operating system's cloud sync folder (e.g. Nextcloud, Dropbox, OneDrive) "
            "to automate off-site backup."
        )
        info_label.setWordWrap(True)
        info_label.setStyleSheet("margin-bottom: 6px;")
        storage_layout.addWidget(info_label)

        # Auto Backup Options
        self.backup_enable_cb = QCheckBox("Automatically export workspace backup on application close")
        self.backup_enable_cb.setStyleSheet("font-weight: bold;")
        storage_layout.addWidget(self.backup_enable_cb)

        backup_box = QHBoxLayout()
        self.backup_dir_input = QLineEdit()
        self.backup_dir_input.setPlaceholderText("Select cloud sync backup folder...")
        self.btn_browse_backup = QPushButton("Browse...")
        backup_box.addWidget(self.backup_dir_input)
        backup_box.addWidget(self.btn_browse_backup)

        self.backup_status_label = QLabel()
        self.backup_status_label.setStyleSheet("font-size: 8.5pt; color: #555;")

        storage_form = QFormLayout()
        storage_form.addRow("Backup Folder:", backup_box)
        storage_form.addRow("Last Backup Status:", self.backup_status_label)

        # Default Export Directory
        export_box = QHBoxLayout()
        self.export_dir_input = QLineEdit()
        self.export_dir_input.setPlaceholderText("Optional: Default directory for batch sheets and JSON exports")
        self.btn_browse_export = QPushButton("Browse...")
        export_box.addWidget(self.export_dir_input)
        export_box.addWidget(self.btn_browse_export)
        storage_form.addRow("Default Export Folder:", export_box)

        storage_layout.addLayout(storage_form)
        storage_layout.addStretch()
        self.tabs.addTab(storage_tab, "Storage && Cloud Sync")

        # Tab 2: Formulation Defaults
        defaults_tab = QWidget()
        defaults_layout = QFormLayout(defaults_tab)
        defaults_layout.setSpacing(12)

        self.precision_combo = QComboBox()
        self.precision_combo.addItem("0.001 g", "0.001")
        self.precision_combo.addItem("0.01 g", "0.01")
        self.precision_combo.addItem("0.1 g", "0.1")
        self.precision_combo.addItem("1 g", "1")
        defaults_layout.addRow("Default Scale Precision:", self.precision_combo)

        self.operator_input = QLineEdit()
        self.operator_input.setPlaceholderText("Default operator name for batch sheets")
        defaults_layout.addRow("Default Operator Name:", self.operator_input)

        self.check_updates_cb = QCheckBox("Automatically check for updates on startup")
        defaults_layout.addRow("", self.check_updates_cb)

        self.tabs.addTab(defaults_tab, "Formulation Defaults")

        # Dialog Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.btn_cancel = QPushButton("Cancel")
        self.btn_save = QPushButton("Save Settings")
        self.btn_save.setStyleSheet("font-weight: bold; padding: 6px 14px;")

        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addWidget(self.btn_save)
        main_layout.addLayout(btn_layout)

        # Connections
        self.btn_browse_backup.clicked.connect(self._browse_backup_dir)
        self.btn_browse_export.clicked.connect(self._browse_export_dir)
        self.backup_enable_cb.toggled.connect(self._update_backup_state)
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_save.clicked.connect(self._save_settings)

    def _load_values(self) -> None:
        self.backup_enable_cb.setChecked(self.config.auto_backup_enabled)
        self.backup_dir_input.setText(self.config.auto_backup_dir)
        self.export_dir_input.setText(self.config.default_export_dir)

        curr_prec = self.config.default_precision
        idx = self.precision_combo.findData(curr_prec)
        if idx >= 0:
            self.precision_combo.setCurrentIndex(idx)

        status = self.config.last_backup_status or "None recorded"
        timestamp = f" ({self.config.last_backup_time})" if self.config.last_backup_time else ""
        self.backup_status_label.setText(f"{status}{timestamp}")
        if "FAIL" in status:
            self.backup_status_label.setStyleSheet("font-size: 8.5pt; color: #dc3545; font-weight: bold;")
        elif "SUCCESS" in status:
            self.backup_status_label.setStyleSheet("font-size: 8.5pt; color: #198754;")

        self.operator_input.setText(self.config.default_operator)
        self.check_updates_cb.setChecked(self.config.check_updates_on_startup)
        self._update_backup_state(self.backup_enable_cb.isChecked())

    def _update_backup_state(self, enabled: bool) -> None:
        self.backup_dir_input.setEnabled(enabled)
        self.btn_browse_backup.setEnabled(enabled)

    def _browse_backup_dir(self) -> None:
        current = self.backup_dir_input.text().strip() or str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "Select Backup Directory", current)
        if selected:
            self.backup_dir_input.setText(selected)

    def _browse_export_dir(self) -> None:
        current = self.export_dir_input.text().strip() or str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "Select Default Export Directory", current)
        if selected:
            self.export_dir_input.setText(selected)

    def _save_settings(self) -> None:
        backup_enabled = self.backup_enable_cb.isChecked()
        backup_dir = self.backup_dir_input.text().strip()
        export_dir = self.export_dir_input.text().strip()

        if backup_enabled:
            if not backup_dir:
                QMessageBox.warning(
                    self,
                    "Invalid Backup Directory",
                    "Please specify a backup directory, or uncheck automated backup.",
                )
                self.tabs.setCurrentIndex(0)
                self.backup_dir_input.setFocus()
                return

            p = Path(backup_dir).expanduser().resolve()
            if not p.exists():
                reply = QMessageBox.question(
                    self,
                    "Create Directory?",
                    f"Backup directory does not exist:\n{p}\n\nWould you like to create it now?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    try:
                        p.mkdir(parents=True, exist_ok=True)
                    except Exception as err:
                        QMessageBox.critical(self, "Directory Error", f"Failed to create directory:\n{err}")
                        return
                else:
                    return

            if not os.access(p, os.W_OK):
                QMessageBox.critical(
                    self,
                    "Permission Denied",
                    f"Backup directory is not writable:\n{p}",
                )
                return

        if export_dir:
            ep = Path(export_dir).expanduser().resolve()
            if not ep.is_dir():
                QMessageBox.warning(
                    self,
                    "Invalid Export Directory",
                    f"Specified export directory does not exist:\n{ep}",
                )
                self.tabs.setCurrentIndex(0)
                self.export_dir_input.setFocus()
                return

        # Commit settings
        self.config.auto_backup_enabled = backup_enabled
        self.config.auto_backup_dir = backup_dir
        self.config.default_export_dir = export_dir
        self.config.default_precision = self.precision_combo.currentData()
        self.config.default_operator = self.operator_input.text().strip()
        self.config.check_updates_on_startup = self.check_updates_cb.isChecked()

        self.accept()
