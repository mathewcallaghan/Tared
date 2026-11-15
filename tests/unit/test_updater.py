"""Unit tests verifying version comparison, release asset discovery, URL security, and update verification."""

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tared.core.updater import (
    compute_file_sha256,
    fetch_latest_release_info,
    find_compatible_asset,
    find_expected_sha256,
    find_expected_sha256_and_signature,
    is_newer_version,
    is_safe_update_url,
    parse_version,
    verify_binary_payload,
)


class TestUpdaterCore(unittest.TestCase):
    def test_parse_version_handles_prefixes_and_variants(self) -> None:
        self.assertEqual(parse_version("1.0.0"), (1, 0, 0, 1, 0))
        self.assertEqual(parse_version("v1.2.3"), (1, 2, 3, 1, 0))
        self.assertEqual(parse_version("V2.0"), (2, 0, 0, 1, 0))
        self.assertEqual(parse_version("v1.0.1-rc1"), (1, 0, 1, 0, 1))
        self.assertEqual(parse_version("invalid"), (0, 0, 0, 1, 0))

    def test_is_newer_version_semver_comparisons(self) -> None:
        self.assertTrue(is_newer_version("1.0.0", "1.0.1"))
        self.assertTrue(is_newer_version("1.0.0", "v1.1.0"))
        self.assertTrue(is_newer_version("1.0.9", "1.1.0"))
        self.assertTrue(is_newer_version("1.9.9", "2.0.0"))
        self.assertTrue(is_newer_version("1.2.0-rc1", "1.2.0"))
        self.assertTrue(is_newer_version("1.2.0-rc1", "1.2.0-rc2"))
        self.assertTrue(is_newer_version("1.0.4", "1.0.5+build42"))
        self.assertTrue(is_newer_version("1.0.0-alpha.1.1", "1.0.0-alpha.1.2"))
        self.assertFalse(is_newer_version("1.0.0-alpha.1.2", "1.0.0-alpha.1.1"))

        self.assertFalse(is_newer_version("1.0.0", "1.0.0"))
        self.assertFalse(is_newer_version("1.0.5", "1.0.5+build42"))
        self.assertFalse(is_newer_version("1.0.1", "1.0.0"))
        self.assertFalse(is_newer_version("2.0.0", "v1.9.9"))
        self.assertFalse(is_newer_version("1.2", "1.2.0"))
        self.assertFalse(is_newer_version("1.2.0", "1.2"))
        self.assertFalse(is_newer_version("1.2.0", "1.2.0-rc1"))
        self.assertTrue(is_newer_version("1.2.0-alpha.2", "1.2.0-rc.1"))
        self.assertTrue(is_newer_version("1.2.0-alpha.2", "1.2.0-beta.1"))
        self.assertFalse(is_newer_version("1.2.0-rc.1", "1.2.0-alpha.2"))

    def test_is_safe_update_url_domain_and_scheme_restrictions(self) -> None:
        self.assertTrue(is_safe_update_url("https://api.github.com/repos/owner/repo/releases/latest"))
        self.assertTrue(is_safe_update_url("https://github.com/owner/repo/releases/download/v1.0.1/app.AppImage"))
        self.assertTrue(is_safe_update_url("https://objects.githubusercontent.com/github-production-release-asset/123"))

        self.assertFalse(is_safe_update_url("http://api.github.com/insecure"))
        self.assertFalse(is_safe_update_url("ftp://github.com/repo"))
        self.assertFalse(is_safe_update_url("file:///etc/passwd"))
        self.assertFalse(is_safe_update_url("https://malicious-github.com/asset.AppImage"))
        self.assertFalse(is_safe_update_url("https://github.com.attacker.com/fake"))
        self.assertFalse(is_safe_update_url(""))

    def test_find_compatible_asset_enforces_strict_version_parity(self) -> None:
        assets = [
            {"name": "source.tar.gz", "browser_download_url": "http://x/s.tar.gz"},
            {"name": "Tared-1.0.1-x86_64.AppImage", "browser_download_url": "http://x/Tared-1.0.1-x86_64.AppImage"},
            {"name": "Tared-1.0.1-aarch64.AppImage", "browser_download_url": "http://x/Tared-1.0.1-aarch64.AppImage"},
            {"name": "Tared-9.9.9-x86_64.AppImage", "browser_download_url": "http://x/Tared-9.9.9-x86_64.AppImage"},
        ]

        found = find_compatible_asset(assets, version="1.0.1", arch="x86_64")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found["name"], "Tared-1.0.1-x86_64.AppImage")

        mismatched = find_compatible_asset(assets, version="1.0.2", arch="x86_64")
        self.assertIsNone(mismatched)

    def test_sha256_computation_and_binary_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            payload = b"\x7fELF" + (bytes([0]) * 2048)
            good_elf = Path(tmpdir) / "app.AppImage"
            good_elf.write_bytes(payload)

            expected_hash = hashlib.sha256(payload).hexdigest()
            self.assertEqual(compute_file_sha256(good_elf), expected_hash)
            self.assertTrue(verify_binary_payload(good_elf, expected_sha256=expected_hash))
            self.assertFalse(verify_binary_payload(good_elf, expected_sha256="wronghash" * 8))

    def test_verify_binary_payload_fail_closed_without_valid_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            payload = b"\x7fELF" + (bytes([0]) * 2048)
            good_elf = Path(tmpdir) / "app.AppImage"
            good_elf.write_bytes(payload)

            self.assertFalse(verify_binary_payload(good_elf, expected_sha256=""))
            self.assertFalse(verify_binary_payload(good_elf, expected_sha256="   "))
            self.assertFalse(verify_binary_payload(good_elf, expected_sha256="abc123"))

    def test_find_expected_sha256_handles_sha256sum_binary_asterisk_format(self) -> None:
        target_name = "Tared-1.0.1-x86_64.AppImage"
        expected_hash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        mock_asset_content = f"{expected_hash} *{target_name}\n"

        assets = [
            {
                "name": "SHA256SUMS",
                "browser_download_url": "https://github.com/mathewcallaghan/Tared/releases/download/v1.0.1/SHA256SUMS",
            }
        ]

        mock_resp = MagicMock()
        mock_resp.geturl.return_value = assets[0]["browser_download_url"]
        mock_resp.read.return_value = mock_asset_content.encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            resolved = find_expected_sha256(assets, target_name)
        self.assertEqual(resolved, expected_hash)

    def test_fetch_latest_release_info_rejects_insecure_origin(self) -> None:
        is_avail, info, msg = fetch_latest_release_info("1.0.0", api_url="http://insecure.api.org/release")
        self.assertFalse(is_avail)
        self.assertIn("Untrusted update API URL", msg)

    def test_fetch_latest_release_info_mock_available_update(self) -> None:
        payload = {
            "tag_name": "v1.0.1",
            "body": (
                "### Bug fixes\n"
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  Tared-1.0.1-x86_64.AppImage"
            ),
            "published_at": "2026-11-09T10:00:00Z",
            "assets": [
                {
                    "name": "Tared-1.0.1-x86_64.AppImage",
                    "browser_download_url": "https://github.com/mathewcallaghan/Tared/releases/download/v1.0.1/Tared-1.0.1-x86_64.AppImage",
                    "size": 60000000,
                }
            ],
        }

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.geturl.return_value = "https://api.github.com/repos/mathewcallaghan/Tared/releases/latest"
        mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            is_avail, info, msg = fetch_latest_release_info("1.0.0")

        self.assertTrue(is_avail)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.version, "1.0.1")
        self.assertEqual(info.asset_name, "Tared-1.0.1-x86_64.AppImage")
        self.assertEqual(info.expected_sha256, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")

    def test_fetch_latest_release_info_rejects_untrusted_redirect(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.geturl.return_value = "https://untrusted-spoof.com/release"
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            is_avail, info, msg = fetch_latest_release_info("1.0.0")

        self.assertFalse(is_avail)
        self.assertIn("Untrusted redirect destination", msg)

    def test_download_worker_cancel_closes_active_response(self) -> None:
        from tared.core.updater import UpdateInfo
        from tared.ui.models.update_worker import DownloadUpdateWorker

        info = UpdateInfo(
            version="1.0.1",
            tag_name="v1.0.1",
            release_notes="notes",
            download_url="https://github.com/owner/repo/releases/download/v1.0.1/app.AppImage",
            asset_name="app.AppImage",
            asset_size=1024,
            published_at="",
            expected_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        worker = DownloadUpdateWorker(info)
        mock_resp = MagicMock()
        worker._active_response = mock_resp
        worker.cancel()
        self.assertTrue(worker._is_cancelled)
        mock_resp.close.assert_called_once()

    def test_ed25519_rfc8032_test_vector_2(self) -> None:
        from tared.core.updater import verify_ed25519

        pubkey = bytes.fromhex("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c")
        message = bytes.fromhex("72")
        signature = bytes.fromhex(
            "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
            "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"
        )
        self.assertTrue(verify_ed25519(pubkey, message, signature))

    def test_find_expected_sha256_and_signature_authenticity(self) -> None:
        import base64

        from tared.core.updater import find_expected_sha256_and_signature

        pubkey_hex = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
        target_name = "Tared-1.0.1-x86_64.AppImage"
        expected_hash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        manifest_text = f"{expected_hash} *{target_name}\n"
        sig_raw = bytes.fromhex(
            "e5564300c360ac729086e2cc806e828a84877f1ebf41586e82ec474d09eed0f6"
            "4c05095bbc8d5c953a3debbc2651939958238cb6e33045472c4d9da0f7234204"
        )

        assets = [
            {
                "name": "SHA256SUMS",
                "browser_download_url": "https://github.com/owner/repo/releases/download/v1.0.1/SHA256SUMS",
            },
            {
                "name": "SHA256SUMS.sig",
                "browser_download_url": "https://github.com/owner/repo/releases/download/v1.0.1/SHA256SUMS.sig",
            },
        ]

        def mock_fetch(url, **kwargs):
            if url.endswith("SHA256SUMS"):
                return manifest_text.encode("utf-8")
            if url.endswith("SHA256SUMS.sig"):
                return base64.b64encode(sig_raw)
            return None

        with patch("tared.core.updater._fetch_asset_bytes", side_effect=mock_fetch):
            # Message is empty in vector 1 so manifest_text will fail verification
            resolved_hash, is_verified = find_expected_sha256_and_signature(
                assets, target_name, pubkey_hex=pubkey_hex
            )
            self.assertEqual(resolved_hash, expected_hash)
            self.assertFalse(is_verified)

    def test_find_expected_sha256_prioritizes_signed_pair_over_standalone_sha(self) -> None:
        target_name = "Tared-1.0.5-x86_64.AppImage"
        hash_standalone = "11" * 32
        hash_signed_manifest = "22" * 32

        assets = [
            {
                "name": f"{target_name}.sha256",
                "browser_download_url": f"https://github.com/owner/repo/releases/download/v1.0.5/{target_name}.sha256",
            },
            {
                "name": "SHA256SUMS",
                "browser_download_url": "https://github.com/owner/repo/releases/download/v1.0.5/SHA256SUMS",
            },
            {
                "name": "SHA256SUMS.sig",
                "browser_download_url": "https://github.com/owner/repo/releases/download/v1.0.5/SHA256SUMS.sig",
            },
        ]

        def mock_fetch(url, **kwargs):
            if url.endswith(f"{target_name}.sha256"):
                return f"{hash_standalone}  {target_name}\n".encode("utf-8")
            if url.endswith("SHA256SUMS"):
                return f"{hash_signed_manifest}  {target_name}\n".encode("utf-8")
            if url.endswith("SHA256SUMS.sig"):
                return b"fake-signature"
            return None

        with patch("tared.core.updater._fetch_asset_bytes", side_effect=mock_fetch):
            resolved_hash, _ = find_expected_sha256_and_signature(assets, target_name)
            self.assertEqual(resolved_hash, hash_signed_manifest)

    def test_restart_appimage_restores_original_ld_library_path(self) -> None:
        from tared.core.updater import restart_appimage

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = Path(tmpdir) / "app.AppImage"
            fake_bin.touch()

            env_patch = {
                "TARED_ORIG_LD_LIBRARY_PATH": "/usr/local/lib:/usr/lib",
                "LD_LIBRARY_PATH": "/tmp/.mount_123/usr/lib:/usr/local/lib:/usr/lib",
            }
            with patch.dict(os.environ, env_patch):
                with patch("os.execv") as mock_execv:
                    restart_appimage(fake_bin)
                    mock_execv.assert_called_once_with(str(fake_bin), [str(fake_bin)] + sys.argv[1:])
                    self.assertEqual(os.environ["LD_LIBRARY_PATH"], "/usr/local/lib:/usr/lib")

            env_patch_empty = {
                "TARED_ORIG_LD_LIBRARY_PATH": "",
                "LD_LIBRARY_PATH": "/tmp/.mount_123/usr/lib",
            }
            with patch.dict(os.environ, env_patch_empty):
                with patch("os.execv"):
                    restart_appimage(fake_bin)
                    self.assertNotIn("LD_LIBRARY_PATH", os.environ)


    def test_update_dialog_cancelling_unlinks_staged_binary(self) -> None:
        from PyQt6.QtWidgets import QApplication

        from tared.ui.views.update_dialog import UpdateDialog

        app = QApplication.instance() or QApplication(["test-updater-ui"])
        self._test_app = app

        dlg = UpdateDialog(auto_check=False)
        with tempfile.NamedTemporaryFile(delete=False) as f:
            staged_file = Path(f.name)
        dlg._staged_binary = staged_file
        self.assertTrue(staged_file.exists())

        dlg._cancel_active_workers()
        self.assertFalse(staged_file.exists())
        self.assertIsNone(dlg._staged_binary)
        dlg.close()

    def test_default_pubkey_does_not_trust_rfc8032_test_vector(self) -> None:
        import base64

        from tared.core.updater import DEFAULT_UPDATER_PUBKEY_HEX, verify_ed25519

        self.assertEqual(DEFAULT_UPDATER_PUBKEY_HEX, "")

        # RFC 8032 test vector 1 (publicly known secret key) must not be trusted by default
        test_vec1_pubkey = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
        sig_raw = bytes.fromhex(
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
            "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
        )
        self.assertTrue(verify_ed25519(bytes.fromhex(test_vec1_pubkey), b"", sig_raw))

        target_name = "Tared-1.0.6-x86_64.AppImage"
        expected_hash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        manifest_text = f"{expected_hash} *{target_name}\n"
        assets = [
            {
                "name": "SHA256SUMS",
                "browser_download_url": "https://github.com/owner/repo/releases/download/v1.0.6/SHA256SUMS",
            },
            {
                "name": "SHA256SUMS.sig",
                "browser_download_url": "https://github.com/owner/repo/releases/download/v1.0.6/SHA256SUMS.sig",
            },
        ]

        def mock_fetch(url, **kwargs):
            if url.endswith("SHA256SUMS"):
                return manifest_text.encode("utf-8")
            if url.endswith("SHA256SUMS.sig"):
                return base64.b64encode(sig_raw)
            return None

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TARED_UPDATER_PUBLIC_KEY", None)
            with patch("tared.core.updater._fetch_asset_bytes", side_effect=mock_fetch):
                resolved_hash, is_verified = find_expected_sha256_and_signature(assets, target_name)
                self.assertEqual(resolved_hash, expected_hash)
                self.assertFalse(is_verified)

    def test_cancelling_blocking_worker_retains_reference_until_finished(self) -> None:
        import threading

        from PyQt6.QtWidgets import QApplication

        from tared.core.updater import UpdateInfo
        from tared.ui.models.update_worker import _ACTIVE_WORKERS
        from tared.ui.views.update_dialog import UpdateDialog

        app = QApplication.instance() or QApplication(["test-updater-ui"])
        self._test_app = app

        entered_urlopen = threading.Event()
        release_urlopen = threading.Event()

        def blocking_urlopen(*args, **kwargs):
            entered_urlopen.set()
            release_urlopen.wait(timeout=5.0)
            raise OSError("Simulated cancelled network call")

        dlg = UpdateDialog(auto_check=False)
        dlg._update_info = UpdateInfo(
            version="1.0.6",
            tag_name="v1.0.6",
            release_notes="notes",
            download_url="https://github.com/owner/repo/releases/download/v1.0.6/app.AppImage",
            asset_name="app.AppImage",
            asset_size=1024,
            published_at="",
            expected_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )

        try:
            with patch("urllib.request.urlopen", side_effect=blocking_urlopen):
                with patch("tared.ui.views.update_dialog.is_running_as_appimage", return_value=True):
                    dlg._on_action_clicked()
                    self.assertTrue(entered_urlopen.wait(timeout=2.0))
                    self.assertEqual(len(_ACTIVE_WORKERS), 1)
                    worker_ref = next(iter(_ACTIVE_WORKERS))

                    # Closing dialog while worker is still blocked must not crash or drop strong ref
                    dlg.close()
                    self.assertIsNone(dlg._download_worker)
                    self.assertIn(worker_ref, _ACTIVE_WORKERS)
        finally:
            release_urlopen.set()
            if _ACTIVE_WORKERS:
                for w in list(_ACTIVE_WORKERS):
                    w.wait(2000)
            app.processEvents()

        self.assertEqual(len(_ACTIVE_WORKERS), 0)

    def test_check_worker_cancel_closes_active_response_and_short_circuits_assets(self) -> None:
        from tared.ui.models.update_worker import CheckUpdateWorker

        worker = CheckUpdateWorker("1.0.0")
        mock_resp = MagicMock()
        worker._set_active_response(mock_resp)
        worker.cancel()
        self.assertTrue(worker._is_cancelled)
        mock_resp.close.assert_called_once()

        payload = {
            "tag_name": "v1.0.6",
            "body": "Release notes",
            "published_at": "2026-11-09T10:00:00Z",
            "assets": [
                {
                    "name": "Tared-1.0.6-x86_64.AppImage",
                    "browser_download_url": (
                        "https://github.com/mathewcallaghan/Tared/releases/download/v1.0.6/Tared-1.0.6-x86_64.AppImage"
                    ),
                    "size": 1000,
                },
                {
                    "name": "SHA256SUMS",
                    "browser_download_url": (
                        "https://github.com/mathewcallaghan/Tared/releases/download/v1.0.6/SHA256SUMS"
                    ),
                },
            ],
        }
        cancelled = False

        api_resp = MagicMock()
        api_resp.status = 200
        api_resp.geturl.return_value = "https://api.github.com/repos/mathewcallaghan/Tared/releases/latest"

        def read_and_cancel(*args, **kwargs):
            nonlocal cancelled
            cancelled = True
            return json.dumps(payload).encode("utf-8")

        api_resp.read.side_effect = read_and_cancel
        api_resp.__enter__.return_value = api_resp

        with patch("urllib.request.urlopen", return_value=api_resp):
            with patch("tared.core.updater._fetch_asset_bytes") as mock_asset_fetch:
                is_avail, info, msg = fetch_latest_release_info(
                    "1.0.0",
                    cancel_check=lambda: cancelled,
                )
                self.assertFalse(is_avail)
                self.assertIsNone(info)
                self.assertIn("cancelled", msg.lower())
                mock_asset_fetch.assert_not_called()

    def test_parse_minisig_rejects_prehashed_ed_header_instead_of_global_signature(self) -> None:
        import base64

        from tared.core.updater import parse_minisig_or_raw_signature

        prehashed_line = base64.b64encode(b"ED" + b"\x00" * 8 + b"\x11" * 64).decode("ascii")
        global_sig_line = base64.b64encode(b"\x22" * 64).decode("ascii")
        minisig_payload = (
            "untrusted comment: signature from minisign secret key\n"
            f"{prehashed_line}\n"
            "trusted comment: timestamp:1700000000\n"
            f"{global_sig_line}\n"
        )
        self.assertIsNone(parse_minisig_or_raw_signature(minisig_payload))

    def test_update_dialog_restart_oserror_displays_error_and_quits_cleanly(self) -> None:
        from PyQt6.QtWidgets import QApplication, QMessageBox

        from tared.core.updater import UpdateInfo
        from tared.ui.views.update_dialog import UpdateDialog

        app = QApplication.instance() or QApplication(["test-updater-ui"])
        self._test_app = app

        dlg = UpdateDialog(auto_check=False)
        self.addCleanup(dlg.close)
        with tempfile.NamedTemporaryFile(delete=False) as f:
            staged_file = Path(f.name)
        dlg._staged_binary = staged_file
        dlg._update_info = UpdateInfo(
            version="1.0.6",
            tag_name="v1.0.6",
            release_notes="notes",
            download_url="https://github.com/owner/repo/releases/download/v1.0.6/Tared-1.0.6-x86_64.AppImage",
            asset_name="Tared-1.0.6-x86_64.AppImage",
            asset_size=1024,
            published_at="",
            expected_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )

        installed_path = Path(staged_file.parent) / "Tared-1.0.6-x86_64.AppImage"
        with patch("tared.ui.views.update_dialog.get_current_appimage_path", return_value=installed_path):
            with patch("PyQt6.QtWidgets.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                with patch("tared.ui.views.update_dialog.apply_appimage_update", return_value=installed_path):
                    with patch(
                        "tared.ui.views.update_dialog.restart_appimage",
                        side_effect=OSError("Permission denied"),
                    ):
                        with patch("PyQt6.QtWidgets.QMessageBox.critical") as mock_crit:
                            with patch.object(app, "quit") as mock_quit:
                                dlg._apply_and_restart()
                                mock_crit.assert_called_once()
                                mock_quit.assert_called_once()

        from PyQt6.QtWidgets import QWidget

        mock_parent = QWidget()
        mock_parent.prepare_for_exit = MagicMock(return_value=True)  # type: ignore[method-assign]
        mock_parent.prepare_restart_resources = MagicMock()  # type: ignore[method-assign]
        dlg_with_parent = UpdateDialog(parent=mock_parent, auto_check=False)
        self.addCleanup(dlg_with_parent.close)
        with tempfile.NamedTemporaryFile(delete=False) as f2:
            staged_file2 = Path(f2.name)
        dlg_with_parent._staged_binary = staged_file2
        dlg_with_parent._update_info = dlg._update_info
        with patch("tared.ui.views.update_dialog.get_current_appimage_path", return_value=installed_path):
            with patch("PyQt6.QtWidgets.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                with patch("tared.ui.views.update_dialog.apply_appimage_update", return_value=installed_path):
                    with patch(
                        "tared.ui.views.update_dialog.restart_appimage",
                        side_effect=OSError("Execv fault"),
                    ):
                        with patch("PyQt6.QtWidgets.QMessageBox.critical"):
                            with patch.object(app, "quit"):
                                dlg_with_parent._apply_and_restart()
                                mock_parent.prepare_restart_resources.assert_called_once()
        staged_file2.unlink(missing_ok=True)
        staged_file.unlink(missing_ok=True)


    def test_download_worker_handles_empty_content_length_header(self) -> None:
        from tared.core.updater import UpdateInfo
        from tared.ui.models.update_worker import DownloadUpdateWorker

        info = UpdateInfo(
            version="1.0.6",
            tag_name="v1.0.6",
            release_notes="notes",
            download_url="https://github.com/owner/repo/releases/download/v1.0.6/app.AppImage",
            asset_name="app.AppImage",
            asset_size=2048,
            published_at="",
            expected_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        worker = DownloadUpdateWorker(info)

        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Length": "   "}
        mock_resp.geturl.return_value = info.download_url
        mock_resp.read.side_effect = [b"", b""]
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            worker.run()

    def test_update_dialog_progress_handles_unknown_total_bytes(self) -> None:
        from PyQt6.QtWidgets import QApplication

        from tared.ui.views.update_dialog import UpdateDialog

        app = QApplication.instance() or QApplication(["test-updater-ui"])
        self._test_app = app

        dlg = UpdateDialog(auto_check=False)
        self.addCleanup(dlg.close)
        dlg._on_download_progress(0, 2 * 1024 * 1024, 0)
        self.assertIn("2.0 MB", dlg.status_title.text())
        self.assertNotIn("0.0 MB", dlg.status_title.text())

    def test_parse_minisig_rejects_non_strict_base64_and_apply_rejects_unsafe_name(self) -> None:
        import base64

        from tared.core.updater import apply_appimage_update, parse_minisig_or_raw_signature

        raw_sig = base64.b64encode(b"\x33" * 64).decode("ascii")
        corrupted = raw_sig[:10] + "!!!" + raw_sig[10:]
        self.assertIsNone(parse_minisig_or_raw_signature(corrupted))

        with tempfile.TemporaryDirectory() as tmpdir:
            payload = b"\x7fELF" + (bytes([0]) * 2048)
            staged = Path(tmpdir) / ".tmp_staged.AppImage"
            staged.write_bytes(payload)
            current = Path(tmpdir) / "Tared-1.0.5-x86_64.AppImage"
            current.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()

            for unsafe in ("../evil.AppImage", "sub/evil.AppImage", "..", "dir\\evil.AppImage"):
                with self.assertRaises(ValueError):
                    apply_appimage_update(staged, current, expected_sha256=digest, new_asset_name=unsafe)

    def test_find_expected_sha256_rejects_non_hex_and_normalizes_uppercase(self) -> None:
        target_name = "Tared-1.0.6-x86_64.AppImage"
        assets = [
            {
                "name": "SHA256SUMS",
                "browser_download_url": "https://github.com/owner/repo/releases/download/v1.0.6/SHA256SUMS",
            }
        ]

        bad_manifest = f"{'z' * 64}  {target_name}\n".encode("utf-8")
        with patch("tared.core.updater._fetch_asset_bytes", return_value=bad_manifest):
            self.assertEqual(find_expected_sha256(assets, target_name), "")

        upper_hex = "E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855"
        good_manifest = f"{upper_hex}  {target_name}\n".encode("utf-8")
        with patch("tared.core.updater._fetch_asset_bytes", return_value=good_manifest):
            self.assertEqual(find_expected_sha256(assets, target_name), upper_hex.lower())

    def test_fetch_latest_release_info_handles_null_and_malformed_assets(self) -> None:
        for assets_val in (None, [None, "bad-entry"]):
            payload = {
                "tag_name": "v1.0.1",
                "body": "Release notes",
                "published_at": "2026-11-09T10:00:00Z",
                "assets": assets_val,
            }
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.geturl.return_value = "https://api.github.com/repos/mathewcallaghan/Tared/releases/latest"
            mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp

            with patch("urllib.request.urlopen", return_value=mock_resp):
                is_avail, info, _ = fetch_latest_release_info("1.0.0")

            self.assertTrue(is_avail)
            self.assertIsNotNone(info)
            assert info is not None
            self.assertEqual(info.download_url, "")
            self.assertEqual(info.asset_name, "")

        bad_payload = {
            "tag_name": "v1.0.1",
            "assets": "not-a-list",
        }
        mock_bad = MagicMock()
        mock_bad.status = 200
        mock_bad.geturl.return_value = "https://api.github.com/repos/mathewcallaghan/Tared/releases/latest"
        mock_bad.read.return_value = json.dumps(bad_payload).encode("utf-8")
        mock_bad.__enter__.return_value = mock_bad

        with patch("urllib.request.urlopen", return_value=mock_bad):
            is_avail, info, msg = fetch_latest_release_info("1.0.0")

        self.assertFalse(is_avail)
        self.assertIsNone(info)
        self.assertIn("malformed assets", msg.lower())

    def test_signed_manifest_missing_target_hash_does_not_mark_body_hash_verified(self) -> None:
        import base64

        from tared.core.updater import _ED25519_B, _ED25519_L, _ed_mul

        seed = b"\x01" * 32
        h = hashlib.sha512(seed).digest()
        a_bytes = bytearray(h[:32])
        a_bytes[0] &= 248
        a_bytes[31] = (a_bytes[31] & 127) | 64
        a = int.from_bytes(a_bytes, "little")
        px, py = _ed_mul(_ED25519_B, a)
        pub_bytes = (py | ((px & 1) << 255)).to_bytes(32, "little")
        pub_hex = pub_bytes.hex()

        manifest_bytes = (b"a" * 64) + b"  Tared-1.0.7-aarch64.AppImage\n"
        r = int.from_bytes(hashlib.sha512(h[32:] + manifest_bytes).digest(), "little") % _ED25519_L
        rx, ry = _ed_mul(_ED25519_B, r)
        r_bytes = (ry | ((rx & 1) << 255)).to_bytes(32, "little")
        k = int.from_bytes(hashlib.sha512(r_bytes + pub_bytes + manifest_bytes).digest(), "little") % _ED25519_L
        sig_b64 = base64.b64encode(r_bytes + ((r + k * a) % _ED25519_L).to_bytes(32, "little"))

        target_name = "Tared-1.0.7-x86_64.AppImage"
        body_hash = "b" * 64
        assets = [
            {
                "name": "SHA256SUMS",
                "browser_download_url": "https://github.com/owner/repo/releases/download/v1.0.7/SHA256SUMS",
            },
            {
                "name": "SHA256SUMS.sig",
                "browser_download_url": "https://github.com/owner/repo/releases/download/v1.0.7/SHA256SUMS.sig",
            },
        ]

        def mock_fetch(url, **kwargs):
            if url.endswith("SHA256SUMS"):
                return manifest_bytes
            if url.endswith("SHA256SUMS.sig"):
                return sig_b64
            return None

        with patch("tared.core.updater._fetch_asset_bytes", side_effect=mock_fetch):
            resolved_hash, is_verified = find_expected_sha256_and_signature(
                assets,
                target_name,
                release_body=f"{body_hash}  {target_name}",
                pubkey_hex=pub_hex,
            )
        self.assertEqual(resolved_hash, body_hash)
        self.assertFalse(is_verified)

    def test_safe_redirect_handler_and_null_asset_fields(self) -> None:
        import urllib.error
        import urllib.request

        from tared.core.updater import _SafeRedirectHandler

        self.assertFalse(is_safe_update_url("https://attacker@github.com/owner/repo"))
        handler = _SafeRedirectHandler()
        req = urllib.request.Request("https://github.com/owner/repo")
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(req, None, 302, "Found", {}, "http://evil.example/payload")

        payload = {
            "tag_name": "v1.0.8",
            "body": "Notes",
            "published_at": None,
            "assets": [
                {"name": None, "browser_download_url": None, "size": None},
                {
                    "name": "Tared-1.0.8-x86_64.AppImage",
                    "browser_download_url": None,
                    "size": None,
                },
            ],
        }
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.geturl.return_value = "https://api.github.com/repos/mathewcallaghan/Tared/releases/latest"
        mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            is_avail, info, _ = fetch_latest_release_info("1.0.0")

        self.assertTrue(is_avail)
        assert info is not None
        self.assertEqual(info.download_url, "")
        self.assertEqual(info.asset_size, 0)
        self.assertEqual(info.published_at, "")

    def test_update_dialog_reports_error_instead_of_up_to_date_on_failure(self) -> None:
        from PyQt6.QtWidgets import QApplication

        from tared.ui.views.update_dialog import UpdateDialog

        app = QApplication.instance() or QApplication(["test-updater-ui"])
        self._test_app = app

        dlg = UpdateDialog(auto_check=False)
        self.addCleanup(dlg.close)

        dlg._on_check_finished(False, None, "Network check failed: timed out")
        self.assertEqual(dlg.status_title.text(), "Update Check Failed")
        self.assertIn("Could not verify", dlg.status_subtitle.text())

        dlg._on_check_finished(False, None, "Tared is up to date (current: v1.0.6, latest: v1.0.6).")
        self.assertEqual(dlg.status_title.text(), "You're up to date!")

        null_tag_resp = MagicMock()
        null_tag_resp.status = 200
        null_tag_resp.geturl.return_value = "https://api.github.com/repos/mathewcallaghan/Tared/releases/latest"
        null_tag_resp.read.return_value = json.dumps({"tag_name": None}).encode("utf-8")
        null_tag_resp.__enter__.return_value = null_tag_resp

        with patch("urllib.request.urlopen", return_value=null_tag_resp):
            is_avail, info, msg = fetch_latest_release_info("1.0.0")
        self.assertFalse(is_avail)
        self.assertIsNone(info)
        self.assertIn("empty version tag", msg.lower())


if __name__ == "__main__":
    unittest.main()
