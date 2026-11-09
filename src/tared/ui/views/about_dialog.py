"""About dialog displaying official branding, application description, and license details."""

from __future__ import annotations

import importlib.resources
import sys
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

import tared

LOGO_FILENAME = "tared-logo.svg"


def load_logo_pixmap() -> QPixmap | None:
    """Load horizontal logo SVG with package resources, frozen AppDir, and dev tree fallbacks."""
    # 1. Standard package resources
    try:
        ref = importlib.resources.files("tared").joinpath("assets", LOGO_FILENAME)
        if ref.is_file():
            with importlib.resources.as_file(ref) as p:
                pix = QPixmap(str(p))
                if not pix.isNull():
                    return pix
    except Exception:
        pass

    # 2. PyInstaller / AppImage bundle
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundled = Path(sys._MEIPASS) / "tared" / "assets" / LOGO_FILENAME
        if bundled.is_file():
            pix = QPixmap(str(bundled))
            if not pix.isNull():
                return pix

    # 3. Development tree fallback
    dev_path = Path(__file__).resolve().parents[2] / "assets" / LOGO_FILENAME
    if dev_path.is_file():
        pix = QPixmap(str(dev_path))
        if not pix.isNull():
            return pix

    return None


class AboutDialog(QDialog):
    """Modal About dialog displaying branding, application description, and license details."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About Tared")
        self.setFixedSize(440, 320)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(18, 18, 18, 18)
        main_layout.setSpacing(14)

        # Integrated White Banner Card
        banner_frame = QFrame()
        banner_frame.setStyleSheet(
            "background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px;"
        )
        banner_layout = QHBoxLayout(banner_frame)
        banner_layout.setContentsMargins(8, 8, 8, 8)

        pixmap = load_logo_pixmap()
        if pixmap and not pixmap.isNull():
            logo_label = QLabel()
            scaled_pix = pixmap.scaledToWidth(280, Qt.TransformationMode.SmoothTransformation)
            logo_label.setPixmap(scaled_pix)
            logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            banner_layout.addWidget(logo_label)
        else:
            title_fallback = QLabel("Tared")
            title_fallback.setStyleSheet("color: #403e3e; font-size: 16pt; font-weight: bold;")
            title_fallback.setAlignment(Qt.AlignmentFlag.AlignCenter)
            banner_layout.addWidget(title_fallback)

        main_layout.addWidget(banner_frame)

        # Version & Details
        version = getattr(tared, "__version__", "1.0.0")
        ver_label = QLabel(f"<b>Version {version}</b>")
        ver_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(ver_label)

        desc_label = QLabel("Precision formulation, discrete mass allocation, and batch execution management.")
        desc_label.setWordWrap(True)
        desc_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(desc_label)

        lic_label = QLabel("Licensed under the GNU General Public License v3 or later.")
        lic_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lic_label.setStyleSheet("font-size: 8.5pt;")
        main_layout.addWidget(lic_label)

        main_layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_close = QPushButton("Close")
        btn_close.setStyleSheet("padding: 5px 20px;")
        btn_close.clicked.connect(self.accept)
        btn_layout.addWidget(btn_close)
        btn_layout.addStretch()
        main_layout.addLayout(btn_layout)
