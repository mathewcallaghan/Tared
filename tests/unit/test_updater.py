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
        self.assertEqual(parse_version("1.0.0"), (1, 0, 0))
        self.assertEqual(parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(parse_version("V2.0"), (2, 0))
        self.assertEqual(parse_version("v1.0.1-rc1"), (1, 0, 1))
        self.assertEqual(parse_version("invalid"), (0,))

    def test_is_newer_version_semver_comparisons(self) -> None:
        self.assertTrue(is_newer_version("1.0.0", "1.0.1"))
        self.assertTrue(is_newer_version("1.0.0", "v1.1.0"))
        self.assertTrue(is_newer_version("1.0.9", "1.1.0"))
        self.assertTrue(is_newer_version("1.9.9", "2.0.0"))

        self.assertFalse(is_newer_version("1.0.0", "1.0.0"))
        self.assertFalse(is_newer_version("1.0.1", "1.0.0"))
        self.assertFalse(is_newer_version("2.0.0", "v1.9.9"))

    def test_is_safe_update_url_domain_and_scheme_restrictions(self) -> None:
        # Valid official endpoints
        self.assertTrue(is_safe_update_url("https://api.github.com/repos/owner/repo/releases/latest"))
        self.assertTrue(is_safe_update_url("https://github.com/owner/repo/releases/download/v1.0.1/app.AppImage"))
        self.assertTrue(is_safe_update_url("https://objects.githubusercontent.com/github-production-release-asset/123"))

        # Insecure or spoofed endpoints
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

        # Disallow mismatched version fallback
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


if __name__ == "__main__":
    unittest.main()
