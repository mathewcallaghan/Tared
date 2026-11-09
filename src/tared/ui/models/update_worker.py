"""Asynchronous QThread workers for non-blocking release queries and AppImage streaming."""

from __future__ import annotations

import urllib.request
import uuid
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from tared.core.updater import (
    MAX_UPDATE_SIZE_BYTES,
    UpdateInfo,
    fetch_latest_release_info,
    get_current_appimage_path,
    is_safe_update_url,
    verify_binary_payload,
)


class CheckUpdateWorker(QThread):
    """Background worker querying GitHub API for release availability."""

    check_finished = pyqtSignal(bool, object, str)  # (is_available, UpdateInfo | None, status_msg)

    def __init__(self, current_version: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.current_version = current_version

    def run(self) -> None:
        is_avail, info, msg = fetch_latest_release_info(self.current_version)
        self.check_finished.emit(is_avail, info, msg)


class DownloadUpdateWorker(QThread):
    """Background worker streaming AppImage release artifact with progress reporting."""

    progress = pyqtSignal(int, int, int)  # (percentage, bytes_read, total_bytes)
    download_finished = pyqtSignal(Path)
    download_failed = pyqtSignal(str)

    def __init__(self, update_info: UpdateInfo, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.update_info = update_info
        self._is_cancelled = False

    def cancel(self) -> None:
        self._is_cancelled = True

    def run(self) -> None:
        if not is_safe_update_url(self.update_info.download_url):
            self.download_failed.emit(f"Untrusted download URL rejected: {self.update_info.download_url}")
            return

        target_appimage = get_current_appimage_path()
        dest_dir = target_appimage.parent if target_appimage else Path.home()
        dest_dir.mkdir(parents=True, exist_ok=True)

        staging_path = dest_dir / f".tmp_tared_update_{uuid.uuid4().hex[:8]}.AppImage"

        try:
            req = urllib.request.Request(
                self.update_info.download_url,
                headers={"User-Agent": "Tared-Updater"},
            )

            with urllib.request.urlopen(req, timeout=15) as response:
                declared_len = int(response.headers.get("Content-Length", self.update_info.asset_size or 0))
                if declared_len > MAX_UPDATE_SIZE_BYTES:
                    self.download_failed.emit(
                        f"Update payload size ({declared_len / (1024*1024):.1f} MB) exceeds maximum allowed limit."
                    )
                    return

                total_bytes = declared_len
                bytes_read = 0
                chunk_size = 64 * 1024

                with open(staging_path, "wb") as out_file:
                    while True:
                        if self._is_cancelled:
                            staging_path.unlink(missing_ok=True)
                            self.download_failed.emit("Download cancelled by user.")
                            return

                        chunk = response.read(chunk_size)
                        if not chunk:
                            break

                        out_file.write(chunk)
                        bytes_read += len(chunk)

                        if bytes_read > MAX_UPDATE_SIZE_BYTES:
                            staging_path.unlink(missing_ok=True)
                            self.download_failed.emit("Update stream exceeded maximum size limit.")
                            return

                        pct = int((bytes_read / total_bytes) * 100) if total_bytes > 0 else 0
                        self.progress.emit(min(pct, 100), bytes_read, total_bytes)

            # Security verification: Fail-closed ELF header and mandatory SHA-256 digest match
            if not self.update_info.expected_sha256:
                staging_path.unlink(missing_ok=True)
                self.download_failed.emit("Release lacks a verified SHA-256 digest. Automated update aborted.")
                return

            if not verify_binary_payload(staging_path, expected_sha256=self.update_info.expected_sha256):
                staging_path.unlink(missing_ok=True)
                self.download_failed.emit(
                    "Downloaded update failed security integrity check (hash mismatch or invalid binary)."
                )
                return

            self.download_finished.emit(staging_path)
        except Exception as err:
            staging_path.unlink(missing_ok=True)
            self.download_failed.emit(f"Download failed: {err}")
