"""Unit tests verifying AboutDialog rendering, branding, and asset resolution."""

import sys
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[2] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from PyQt6.QtWidgets import QApplication

from tared.ui.views.about_dialog import AboutDialog, load_logo_pixmap


class TestAboutDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if QApplication.instance() is None:
            cls.app = QApplication(["test-about-runner"])
        else:
            cls.app = QApplication.instance()

    def test_logo_pixmap_loader_resolves_asset(self) -> None:
        pix = load_logo_pixmap()
        self.assertIsNotNone(pix)
        assert pix is not None
        self.assertFalse(pix.isNull())
        self.assertGreater(pix.width(), 50)

    def test_about_dialog_initialization(self) -> None:
        dlg = AboutDialog()
        self.assertEqual(dlg.windowTitle(), "About Tared")
        self.assertGreaterEqual(dlg.width(), 400)
        dlg.close()


if __name__ == "__main__":
    unittest.main()
