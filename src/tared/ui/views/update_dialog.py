"""Modal dialog managing release inspection, download progress, and AppImage installation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PyQt6.QtCore import QSettings, QUrl
from PyQt6.QtGui import QCloseEvent, QDesktopServices
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

import tared
from tared.core.updater import (
    RELEASES_WEB_URL,
    UpdateInfo,
    apply_appimage_update,
    get_current_appimage_path,
    is_running_as_appimage,
    restart_appimage,
)
from tared.ui.models.update_worker import CheckUpdateWorker, DownloadUpdateWorker, track_worker


class UpdateDialog(QDialog):
    """User interface for release verification, changelog presentation, and automated update."""

    def __init__(self, parent: Any = None, auto_check: bool = True) -> None:
        super().__init__(parent)
        self.setWindowTitle("Software Update")
        self.resize(540, 420)
        self.setMinimumWidth(480)

        self._current_version = getattr(tared, "__version__", "1.0.0")
        self._update_info: UpdateInfo | None = None
        self._check_worker: CheckUpdateWorker | None = None
        self._download_worker: DownloadUpdateWorker | None = None
        self._staged_binary: Path | None = None

        self._setup_ui()

        if auto_check:
            self._start_update_check()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(18, 18, 18, 18)

        header_card = QFrame()
        header_card.setStyleSheet(
            "background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 8px;"
        )
        header_layout = QVBoxLayout(header_card)

        self.status_title = QLabel("Checking for updates...")
        self.status_title.setStyleSheet("font-size: 11pt; font-weight: bold; color: #1e293b;")
        self.status_subtitle = QLabel(f"Installed Version: v{self._current_version}")
        self.status_subtitle.setStyleSheet("color: #64748b; font-size: 9pt;")

        header_layout.addWidget(self.status_title)
        header_layout.addWidget(self.status_subtitle)
        layout.addWidget(header_card)

        notes_label = QLabel("Release Notes:")
        notes_label.setStyleSheet("font-weight: bold; margin-top: 4px;")
        layout.addWidget(notes_label)

        self.notes_browser = QTextBrowser()
        self.notes_browser.setOpenExternalLinks(True)
        self.notes_browser.setPlaceholderText("Release notes will appear here once retrieved.")
        layout.addWidget(self.notes_browser)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        btn_layout = QHBoxLayout()
        self.btn_action = QPushButton("Check Again")
        self.btn_web = QPushButton("View on GitHub")
        self.btn_close = QPushButton("Close")

        self.btn_action.setStyleSheet("font-weight: bold; padding: 6px 14px;")
        self.btn_web.setVisible(False)

        btn_layout.addWidget(self.btn_action)
        btn_layout.addWidget(self.btn_web)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_close)
        layout.addLayout(btn_layout)

        self.btn_action.clicked.connect(self._on_action_clicked)
        self.btn_web.clicked.connect(self._open_web_releases)
        self.btn_close.clicked.connect(self._on_close_clicked)

    def _start_update_check(self) -> None:
        if self._check_worker is not None:
            try:
                self._check_worker.cancel()
                try:
                    self._check_worker.check_finished.disconnect(self._on_check_finished)
                except (RuntimeError, TypeError):
                    pass
                if self._check_worker.isRunning():
                    self._check_worker.wait(50)
            except RuntimeError:
                pass
            self._check_worker = None

        self.status_title.setText("Querying GitHub for latest release...")
        self.btn_action.setEnabled(False)
        self.btn_action.setText("Checking...")

        self._check_worker = CheckUpdateWorker(self._current_version, parent=None)
        self._check_worker.check_finished.connect(self._on_check_finished)
        track_worker(self._check_worker)
        self._check_worker.start()

    def _on_check_finished(self, is_available: bool, info: UpdateInfo | None, message: str) -> None:
        self.btn_action.setEnabled(True)

        if not is_available or info is None:
            if message.startswith("Tared is up to date"):
                self.status_title.setText("You're up to date!")
                self.status_subtitle.setText(f"Tared v{self._current_version} is currently the newest version.")
            else:
                self.status_title.setText("Update Check Failed")
                self.status_subtitle.setText(f"Could not verify latest release for v{self._current_version}.")
            self.notes_browser.setText(message)
            self.btn_action.setText("Check Again")
            self.btn_web.setVisible(True)
            return

        self._update_info = info
        auth_badge = " [Authentic ✓]" if info.is_signature_verified else ""
        self.status_title.setText(f"Update Available: v{info.version}{auth_badge}")
        published_note = f" (Released: {info.published_at[:10]})" if info.published_at else ""
        auth_note = " • Verified Ed25519 signature" if info.is_signature_verified else ""
        self.status_subtitle.setText(
            f"Installed: v{self._current_version} → Available: {info.tag_name}{published_note}{auth_note}"
        )
        self.notes_browser.setMarkdown(info.release_notes)
        self.btn_web.setVisible(True)

        can_auto_install = bool(is_running_as_appimage() and info.download_url and info.expected_sha256)
        if can_auto_install:
            size_mb = (info.asset_size / (1024 * 1024)) if info.asset_size else 0
            size_str = f" ({size_mb:.1f} MB)" if size_mb > 0 else ""
            self.btn_action.setText(f"Download & Install Update{size_str}")
        else:
            if is_running_as_appimage() and info.download_url and not info.expected_sha256:
                sub = self.status_subtitle.text()
                self.status_subtitle.setText(f"{sub} • Automated install disabled (no SHA-256 published)")
            self.btn_action.setText("View Release Page")

    def _on_action_clicked(self) -> None:
        if self._staged_binary is not None:
            self._apply_and_restart()
            return

        if self._update_info is None:
            self._start_update_check()
            return

        if not is_running_as_appimage() or not self._update_info.download_url or not self._update_info.expected_sha256:
            self._open_web_releases()
            return

        self.btn_action.setEnabled(False)
        self.btn_action.setText("Downloading...")
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)

        self._download_worker = DownloadUpdateWorker(self._update_info, parent=None)
        self._download_worker.progress.connect(self._on_download_progress)
        self._download_worker.download_finished.connect(self._on_download_finished)
        self._download_worker.download_failed.connect(self._on_download_failed)
        track_worker(self._download_worker)
        self._download_worker.start()

    def _on_download_progress(self, percent: int, bytes_read: int, total_bytes: int) -> None:
        self.progress_bar.setValue(percent)
        mb_read = bytes_read / (1024 * 1024)
        if total_bytes > 0:
            mb_total = total_bytes / (1024 * 1024)
            self.status_title.setText(f"Downloading update: {mb_read:.1f} MB of {mb_total:.1f} MB ({percent}%)")
        else:
            self.status_title.setText(f"Downloading update: {mb_read:.1f} MB...")

    def _on_download_finished(self, staged_path: Path) -> None:
        self._staged_binary = staged_path
        self.progress_bar.setValue(100)
        self.status_title.setText("Download Complete!")
        self.status_subtitle.setText("Update verified successfully. Ready to restart.")

        self.btn_action.setEnabled(True)
        self.btn_action.setText("Restart & Apply Update")
        self.btn_action.setStyleSheet(
            "background-color: #0f766e; color: #ffffff; font-weight: bold; padding: 6px 14px;"
        )

    def _on_download_failed(self, error_msg: str) -> None:
        self.progress_bar.setVisible(False)
        self.btn_action.setEnabled(True)
        self.btn_action.setText("Retry Download")
        QMessageBox.critical(self, "Download Error", f"Failed to download update:\n{error_msg}")

    def _apply_and_restart(self) -> None:
        current_appimage = get_current_appimage_path()
        if not current_appimage or not self._staged_binary:
            return

        if self._update_info is None or not self._update_info.expected_sha256:
            self._cleanup_staged_binary()
            QMessageBox.warning(
                self, "Update Incomplete", "Release metadata is missing. Please retry the update check."
            )
            return

        reply = QMessageBox.question(
            self,
            "Restart Application",
            "Tared will now close, save preferences, and restart to finalize the update. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        parent_window = self.parent()
        if parent_window is not None and hasattr(parent_window, "prepare_for_exit"):
            if not parent_window.prepare_for_exit():
                return

        try:
            final_path = apply_appimage_update(
                self._staged_binary,
                current_appimage,
                expected_sha256=self._update_info.expected_sha256,
                new_asset_name=self._update_info.asset_name,
            )
            self._staged_binary = None
        except Exception as err:
            QMessageBox.critical(self, "Update Failed", f"Could not replace AppImage binary:\n{err}")
            return

        if parent_window is not None and hasattr(parent_window, "prepare_restart_resources"):
            parent_window.prepare_restart_resources()
        elif parent_window is not None and hasattr(parent_window, "finalize_exit_for_restart"):
            parent_window.finalize_exit_for_restart()
        elif parent_window is not None and hasattr(parent_window, "close"):
            if not parent_window.close():
                return

        if (
            parent_window is not None
            and hasattr(parent_window, "config")
            and hasattr(parent_window.config, "_settings")
        ):
            parent_window.config._settings.sync()
        else:
            QSettings().sync()

        try:
            restart_appimage(final_path)
        except OSError as err:
            from PyQt6.QtWidgets import QApplication

            QMessageBox.critical(
                self,
                "Restart Failed",
                f"Update was installed to:\n{final_path}\n\n"
                f"However, Tared could not restart automatically ({err}).\n"
                "Please relaunch the application manually.",
            )
            self.accept()
            app = QApplication.instance()
            if app is not None:
                app.quit()

    def _open_web_releases(self) -> None:
        QDesktopServices.openUrl(QUrl(RELEASES_WEB_URL))

    def _cleanup_staged_binary(self) -> None:
        if self._staged_binary is not None:
            try:
                self._staged_binary.unlink(missing_ok=True)
            except OSError:
                pass
            self._staged_binary = None

    def _cancel_active_workers(self) -> None:
        if self._download_worker is not None:
            try:
                self._download_worker.cancel()
                for sig in (
                    self._download_worker.progress,
                    self._download_worker.download_finished,
                    self._download_worker.download_failed,
                ):
                    try:
                        sig.disconnect()
                    except (RuntimeError, TypeError):
                        pass
                if self._download_worker.isRunning():
                    self._download_worker.wait(50)
            except RuntimeError:
                pass
            self._download_worker = None

        if self._check_worker is not None:
            try:
                self._check_worker.cancel()
                try:
                    self._check_worker.check_finished.disconnect(self._on_check_finished)
                except (RuntimeError, TypeError):
                    pass
                if self._check_worker.isRunning():
                    self._check_worker.wait(50)
            except RuntimeError:
                pass
            self._check_worker = None

        self._cleanup_staged_binary()

    def closeEvent(self, event: QCloseEvent | None) -> None:
        self._cancel_active_workers()
        if event is not None:
            event.accept()

    def reject(self) -> None:
        self._cancel_active_workers()
        super().reject()

    def _on_close_clicked(self) -> None:
        self._cancel_active_workers()
        self.accept()
