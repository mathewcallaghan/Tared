"""Test suite initialization ensuring src root resolution across test runners."""

import sys
import tempfile
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

try:
    from PyQt6.QtCore import QSettings

    _test_settings_dir = tempfile.TemporaryDirectory()
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, _test_settings_dir.name)
except ImportError:
    pass
