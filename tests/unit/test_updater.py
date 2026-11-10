"""Unit tests verifying version comparison, release asset discovery, URL security, and update verification."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tared.core.updater import (
    compute_file_sha256,
    fetch_latest_release_info,
    find_compatible_asset,
    find_expected_sha256,
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

        self.assertFalse(is_newer_version("1.0.0", "1.0.0"))
        self.assertFalse(is_newer_version("1.0.1", "1.0.0"))
        self.assertFalse(is_newer_version("2.0.0", "v1.9.9"))
        self.assertFalse(is_newer_version("1.2", "1.2.0"))
        self.assertFalse(is_newer_version("1.2.0", "1.2"))
        self.assertFalse(is_newer_version("1.2.0", "1.2.0-rc1"))

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


if __name__ == "__main__":
    unittest.main()
