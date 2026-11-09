"""Application configuration manager backed by QSettings for desktop preferences."""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtCore import QSettings


class ConfigManager:
    """Manages persistent application configuration with safe defaults."""

    KEY_AUTO_BACKUP_ENABLED = "storage/auto_backup_enabled"
    KEY_AUTO_BACKUP_DIR = "storage/auto_backup_dir"
    KEY_DEFAULT_EXPORT_DIR = "storage/default_export_dir"
    KEY_DEFAULT_OPERATOR = "production/default_operator"
    KEY_DEFAULT_PRECISION = "production/default_precision"
    KEY_CHECK_UPDATES_ON_STARTUP = "general/check_updates_on_startup"
    KEY_LAST_BACKUP_TIME = "storage/last_backup_time"
    KEY_LAST_BACKUP_STATUS = "storage/last_backup_status"

    VALID_PRECISIONS = ("0.001", "0.01", "0.1", "1")

    def __init__(self, settings: QSettings | None = None) -> None:
        if settings is not None:
            self._settings = settings
        else:
            self._settings = QSettings("Tared", "Tared")

    @property
    def auto_backup_enabled(self) -> bool:
        """Whether workspace backup is automatically exported on close (disabled by default)."""
        val = self._settings.value(self.KEY_AUTO_BACKUP_ENABLED, False)
        if isinstance(val, bool):
            return val
        return str(val).lower() in ("true", "1", "yes")

    @auto_backup_enabled.setter
    def auto_backup_enabled(self, value: bool) -> None:
        self._settings.setValue(self.KEY_AUTO_BACKUP_ENABLED, bool(value))

    @property
    def auto_backup_dir(self) -> str:
        """Directory path where automatic workspace backups are written."""
        return str(self._settings.value(self.KEY_AUTO_BACKUP_DIR, "") or "").strip()

    @auto_backup_dir.setter
    def auto_backup_dir(self, value: str) -> None:
        self._settings.setValue(self.KEY_AUTO_BACKUP_DIR, str(value).strip())

    @property
    def default_export_dir(self) -> str:
        """Default directory path to pre-populate file dialogs for PDF and JSON exports."""
        return str(self._settings.value(self.KEY_DEFAULT_EXPORT_DIR, "") or "").strip()

    @default_export_dir.setter
    def default_export_dir(self, value: str) -> None:
        self._settings.setValue(self.KEY_DEFAULT_EXPORT_DIR, str(value).strip())

    def get_resolved_export_dir(self) -> Path:
        """Return configured default export directory if valid, otherwise Path.home()."""
        raw = self.default_export_dir
        if raw:
            p = Path(raw).expanduser().resolve()
            if p.is_dir():
                return p
        return Path.home().resolve()

    @property
    def default_operator(self) -> str:
        """Default compounding operator name."""
        return str(self._settings.value(self.KEY_DEFAULT_OPERATOR, "") or "").strip()

    @default_operator.setter
    def default_operator(self, value: str) -> None:
        self._settings.setValue(self.KEY_DEFAULT_OPERATOR, str(value).strip())

    @property
    def default_precision(self) -> str:
        """Default scale precision quantum string (0.001, 0.01, 0.1, or 1)."""
        val = str(self._settings.value(self.KEY_DEFAULT_PRECISION, "0.01") or "").strip()
        return val if val in self.VALID_PRECISIONS else "0.01"

    @default_precision.setter
    def default_precision(self, value: str) -> None:
        val = str(value).strip()
        if val in self.VALID_PRECISIONS:
            self._settings.setValue(self.KEY_DEFAULT_PRECISION, val)
        else:
            raise ValueError(f"Invalid scale precision: '{value}'. Must be one of {self.VALID_PRECISIONS}.")

    @property
    def check_updates_on_startup(self) -> bool:
        """Whether to automatically check for updates on application launch."""
        val = self._settings.value(self.KEY_CHECK_UPDATES_ON_STARTUP, True)
        if isinstance(val, bool):
            return val
        return str(val).lower() in ("true", "1", "yes")

    @check_updates_on_startup.setter
    def check_updates_on_startup(self, value: bool) -> None:
        self._settings.setValue(self.KEY_CHECK_UPDATES_ON_STARTUP, bool(value))

    @property
    def last_backup_time(self) -> str:
        """Timestamp of the last attempted automatic backup."""
        return str(self._settings.value(self.KEY_LAST_BACKUP_TIME, "") or "").strip()

    @last_backup_time.setter
    def last_backup_time(self, value: str) -> None:
        self._settings.setValue(self.KEY_LAST_BACKUP_TIME, str(value).strip())

    @property
    def last_backup_status(self) -> str:
        """Status or error description of the last automatic backup."""
        return str(self._settings.value(self.KEY_LAST_BACKUP_STATUS, "") or "").strip()

    @last_backup_status.setter
    def last_backup_status(self, value: str) -> None:
        self._settings.setValue(self.KEY_LAST_BACKUP_STATUS, str(value).strip())

    def record_backup_result(self, success: bool, message: str = "") -> None:
        """Persist timestamp and status of an automated backup attempt."""
        from datetime import datetime, timezone

        self.last_backup_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        self.last_backup_status = "SUCCESS" if success else f"FAILED: {message}"

    def is_backup_configured(self) -> bool:
        """Check if automated backup is enabled and target folder exists and is writable."""
        if not self.auto_backup_enabled:
            return False
        raw_dir = self.auto_backup_dir
        if not raw_dir:
            return False
        p = Path(raw_dir).expanduser().resolve()
        return p.is_dir() and os.access(p, os.W_OK)
