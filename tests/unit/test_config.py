"""Unit tests verifying ConfigManager defaults, persistence, directory validation, and isolated QSettings."""

import tempfile
import unittest
from pathlib import Path

from PyQt6.QtCore import QSettings

from tared.config import ConfigManager


class TestConfigManager(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ini_path = Path(self.temp_dir.name) / "test_settings.ini"
        self.qsettings = QSettings(str(self.ini_path), QSettings.Format.IniFormat)
        self.config = ConfigManager(self.qsettings)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_default_values_safe_and_disabled(self) -> None:
        self.assertFalse(self.config.auto_backup_enabled)
        self.assertEqual(self.config.auto_backup_dir, "")
        self.assertEqual(self.config.default_export_dir, "")
        self.assertEqual(self.config.default_operator, "")
        self.assertEqual(self.config.default_precision, "0.01")
        self.assertFalse(self.config.is_backup_configured())
        self.assertEqual(self.config.last_backup_time, "")
        self.assertEqual(self.config.last_backup_status, "")

    def test_record_backup_result_persists_time_and_status(self) -> None:
        self.config.record_backup_result(True)
        self.assertEqual(self.config.last_backup_status, "SUCCESS")
        self.assertTrue(len(self.config.last_backup_time) > 0)

        self.config.record_backup_result(False, "Disk full")
        self.assertIn("FAILED: Disk full", self.config.last_backup_status)

    def test_property_persistence(self) -> None:
        self.config.auto_backup_enabled = True
        self.config.auto_backup_dir = "/tmp/sync_dir"
        self.config.default_export_dir = "/tmp/exports"
        self.config.default_operator = "Chemist Alex"
        self.config.default_precision = "0.001"

        # Re-initialize from the same storage
        reloaded = ConfigManager(QSettings(str(self.ini_path), QSettings.Format.IniFormat))
        self.assertTrue(reloaded.auto_backup_enabled)
        self.assertEqual(reloaded.auto_backup_dir, "/tmp/sync_dir")
        self.assertEqual(reloaded.default_export_dir, "/tmp/exports")
        self.assertEqual(reloaded.default_operator, "Chemist Alex")
        self.assertEqual(reloaded.default_precision, "0.001")

    def test_is_backup_configured_validation(self) -> None:
        # 1. Disabled
        self.config.auto_backup_enabled = False
        self.config.auto_backup_dir = self.temp_dir.name
        self.assertFalse(self.config.is_backup_configured())

        # 2. Enabled but empty dir
        self.config.auto_backup_enabled = True
        self.config.auto_backup_dir = ""
        self.assertFalse(self.config.is_backup_configured())

        # 3. Enabled but non-existent dir
        self.config.auto_backup_dir = str(Path(self.temp_dir.name) / "non_existent_folder")
        self.assertFalse(self.config.is_backup_configured())

        # 4. Enabled with valid existing writable dir
        valid_dir = Path(self.temp_dir.name) / "valid_sync"
        valid_dir.mkdir()
        self.config.auto_backup_dir = str(valid_dir)
        self.assertTrue(self.config.is_backup_configured())

    def test_resolved_export_dir_fallback(self) -> None:
        self.assertEqual(self.config.get_resolved_export_dir(), Path.home().resolve())

        valid_export = Path(self.temp_dir.name) / "export_target"
        valid_export.mkdir()
        self.config.default_export_dir = str(valid_export)
        self.assertEqual(self.config.get_resolved_export_dir(), valid_export.resolve())

    def test_invalid_precision_raises_error(self) -> None:
        with self.assertRaises(ValueError):
            self.config.default_precision = "0.05"


if __name__ == "__main__":
    unittest.main()
