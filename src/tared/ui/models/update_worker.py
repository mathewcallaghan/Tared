"""Asynchronous QThread workers for non-blocking release queries and AppImage streaming."""

from __future__ import annotations

import urllib.request
import uuid
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from tared.core.updater import (
    MAX_UPDATE_SIZE_BYTES,
    UpdateInfo,
    fetch_latest_release_info,
    get_current_appimage_path,
    is_safe_update_url,
    open_safe_update_url,
    verify_binary_payload,
)

_ACTIVE_WORKERS: set[QThread] = set()


def _release_worker(worker: QThread) -> None:
    """Release tracked worker reference and schedule safe C++ cleanup after thread completion."""
    _ACTIVE_WORKERS.discard(worker)
    try:
        worker.deleteLater()
    except RuntimeError:
        pass


def track_worker(worker: QThread) -> None:
    """Retain a strong Python reference to a worker until its QThread finishes."""
    _ACTIVE_WORKERS.add(worker)
    worker.finished.connect(lambda w=worker: _release_worker(w))


def cancel_and_wait_active_workers(timeout_ms: int = 12000) -> None:
    """Cancel and await any still-running tracked workers before application teardown."""
    for worker in list(_ACTIVE_WORKERS):
        cancel_fn = getattr(worker, "cancel", None)
        if callable(cancel_fn):
            try:
                cancel_fn()
            except RuntimeError:
                pass
        try:
            if worker.isRunning():
                worker.wait(timeout_ms)
        except RuntimeError:
            pass
        try:
            if not worker.isRunning():
                _ACTIVE_WORKERS.discard(worker)
        except RuntimeError:
            _ACTIVE_WORKERS.discard(worker)


class CheckUpdateWorker(QThread):
    """Background worker querying GitHub API for release availability."""

    check_finished = pyqtSignal(bool, object, str)  # (is_available, UpdateInfo | None, status_msg)

    def __init__(self, current_version: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.current_version = current_version
        self._is_cancelled = False
        self._active_response: Any = None

    def _set_active_response(self, response: Any) -> None:
        self._active_response = response

    def cancel(self) -> None:
        self._is_cancelled = True
        resp = self._active_response
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    def run(self) -> None:
        if self._is_cancelled:
            return
        is_avail, info, msg = fetch_latest_release_info(
            self.current_version,
            cancel_check=lambda: self._is_cancelled,
            on_response=self._set_active_response,
        )
        if not self._is_cancelled:
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
        self._active_response: Any = None

    def cancel(self) -> None:
        self._is_cancelled = True
        resp = self._active_response
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    def run(self) -> None:
        if self._is_cancelled:
            return

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

            with open_safe_update_url(req, timeout=8) as response:
                self._active_response = response
                if self._is_cancelled:
                    staging_path.unlink(missing_ok=True)
                    return

                resp_url = getattr(response, "geturl", lambda: None)()
                if isinstance(resp_url, str) and not is_safe_update_url(resp_url):
                    if not self._is_cancelled:
                        self.download_failed.emit(f"Redirect to untrusted update host rejected: {resp_url}")
                    return

                header_len = response.headers.get("Content-Length")
                declared_len = (
                    int(header_len)
                    if header_len and header_len.strip().isdigit()
                    else int(self.update_info.asset_size or 0)
                )
                if declared_len > MAX_UPDATE_SIZE_BYTES:
                    if not self._is_cancelled:
                        size_mb = declared_len / (1024 * 1024)
                        self.download_failed.emit(
                            f"Update payload size ({size_mb:.1f} MB) exceeds maximum allowed limit."
                        )
                    return

                total_bytes = declared_len
                bytes_read = 0
                chunk_size = 64 * 1024

                with open(staging_path, "wb") as out_file:
                    while True:
                        if self._is_cancelled:
                            staging_path.unlink(missing_ok=True)
                            return

                        chunk = response.read(chunk_size)
                        if not chunk:
                            break

                        out_file.write(chunk)
                        bytes_read += len(chunk)

                        if bytes_read > MAX_UPDATE_SIZE_BYTES:
                            staging_path.unlink(missing_ok=True)
                            if not self._is_cancelled:
                                self.download_failed.emit("Update stream exceeded maximum size limit.")
                            return

                        pct = int((bytes_read / total_bytes) * 100) if total_bytes > 0 else 0
                        if not self._is_cancelled:
                            self.progress.emit(min(pct, 100), bytes_read, total_bytes)

            if self._is_cancelled:
                staging_path.unlink(missing_ok=True)
                return

            if not self.update_info.expected_sha256:
                staging_path.unlink(missing_ok=True)
                if not self._is_cancelled:
                    self.download_failed.emit("Release lacks a verified SHA-256 digest. Automated update aborted.")
                return

            if not verify_binary_payload(staging_path, expected_sha256=self.update_info.expected_sha256):
                staging_path.unlink(missing_ok=True)
                if not self._is_cancelled:
                    self.download_failed.emit(
                        "Downloaded update failed security integrity check (hash mismatch or invalid binary)."
                    )
                return

            if self._is_cancelled:
                staging_path.unlink(missing_ok=True)
                return

            self.download_finished.emit(staging_path)
        except Exception as err:
            staging_path.unlink(missing_ok=True)
            if not self._is_cancelled:
                self.download_failed.emit(f"Download failed: {err}")
        finally:
            self._active_response = None
