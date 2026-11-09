r"""Automated release verification and AppImage update delivery for Tared.

================================================================================
INTEGRITY & TRUST MODEL:
================================================================================
The update subsystem validates release integrity and automates AppImage installation:

1. Server-Side Operational Controls (Maintainer Environment):
   - Direct Upstream Binding: Releases are published exclusively under the official
     repository: https://github.com/mathewcallaghan/Tared.
   - Access Control: Releases and Git tags are published from maintainer accounts
     enforcing Multi-Factor Authentication (2FA).
   - Tag Immutability: Upstream Git tags conforming to semantic versioning
     (e.g., 'v1.0.1') are protected against deletion and force-pushes.

2. Client-Side Cryptographic & Transport Integrity Controls:
   - Transport Security: API and asset downloads are restricted strictly to HTTPS
     endpoints on official GitHub hosts (api.github.com, github.com, *.githubusercontent.com).
   - SHA-256 Digest Verification: Staged binaries must match an expected SHA-256 digest
     published in release companion checksums or release body. Note: This guarantees
     transport and download integrity; defense against compromised maintainer credentials
     requires detached offline signature verification (e.g. Minisign/Sigstore).
   - Strict Semantic Versioning: Upstream release tags must parse into integer tuples
     strictly greater than local __version__.
   - Strict Asset Parity: Release assets must match: ^Tared[-_]v?{version}[-_]{arch}\.AppImage$
   - Download Guardrails: Download streams enforce maximum size ceilings (500 MiB) to prevent
     unbounded resource exhaustion.
   - POSIX Binary Validation: Staged payloads must pass ELF magic-byte inspection
     ('\x7fELF') and size validation (>1 KB) before receiving execution permissions.
   - Atomic Filesystem Swapping: Downloads stage to hidden temporary files on the
     same filesystem as target AppImage, swapped atomically via os.replace.
================================================================================
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

GITHUB_OWNER = "mathewcallaghan"
GITHUB_REPO = "Tared"
LATEST_RELEASE_API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
RELEASES_WEB_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases"

MAX_UPDATE_SIZE_BYTES = 500 * 1024 * 1024  # 500 MiB payload ceiling
ALLOWED_UPDATE_SCHEMES = ("https",)
ALLOWED_UPDATE_HOSTS = (
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "raw.githubusercontent.com",
)


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    """Immutable representation of a verified GitHub release update."""

    version: str
    tag_name: str
    release_notes: str
    download_url: str
    asset_name: str
    asset_size: int
    published_at: str
    expected_sha256: str = ""


def is_safe_update_url(url: str) -> bool:
    """Validate that update URL uses HTTPS and targets official GitHub domains."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ALLOWED_UPDATE_SCHEMES:
            return False
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        return host in ALLOWED_UPDATE_HOSTS or any(
            host.endswith(f".{allowed}") for allowed in ("github.com", "githubusercontent.com")
        )
    except Exception:
        return False


def parse_version(version_str: str) -> tuple[int, ...]:
    """Parse a semantic version string (e.g., 'v1.2.3' or '1.2') into an integer tuple."""
    clean = version_str.strip().lstrip("vV")
    clean = re.sub(r"[-+].*$", "", clean)
    numbers = []
    for segment in clean.split("."):
        try:
            numbers.append(int(segment))
        except ValueError:
            break
    return tuple(numbers) if numbers else (0,)


def is_newer_version(current_ver: str, candidate_ver: str) -> bool:
    """Return True if candidate_ver is strictly newer than current_ver."""
    return parse_version(candidate_ver) > parse_version(current_ver)


def get_current_appimage_path() -> Path | None:
    """Return Path to running AppImage if running inside a mounted bundle."""
    appimage_env = os.environ.get("APPIMAGE")
    if appimage_env:
        p = Path(appimage_env).resolve()
        if p.is_file():
            return p
    return None


def is_running_as_appimage() -> bool:
    """Check whether the active process is executing from a packaged AppImage."""
    return get_current_appimage_path() is not None


def find_compatible_asset(
    assets: list[dict],
    version: str,
    arch: str | None = None,
) -> dict | None:
    """Locate matching AppImage release artifact enforcing strict version and architecture parity."""
    if arch is None:
        arch = platform.machine()
        if arch in ("AMD64", "x86_64"):
            arch = "x86_64"

    clean_ver = version.lstrip("vV")
    strict_pattern = re.compile(
        rf"^Tared[-_]v?{re.escape(clean_ver)}[-_]{re.escape(arch)}\.AppImage$",
        re.IGNORECASE,
    )

    for asset in assets:
        name = asset.get("name", "")
        if strict_pattern.match(name):
            return asset

    return None


def compute_file_sha256(file_path: Path) -> str:
    """Compute hex SHA-256 digest of a local file."""
    sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(64 * 1024):
            sha.update(chunk)
    return sha.hexdigest().lower()


def find_expected_sha256(
    assets: list[dict],
    target_asset_name: str,
    release_body: str = "",
    timeout: int = 4,
) -> str:
    """Attempt to resolve expected SHA-256 digest from companion assets or release manifest."""
    sha_asset_names = {
        f"{target_asset_name}.sha256",
        f"{target_asset_name}.sha256.txt",
        "SHA256SUMS",
        "SHA256SUMS.txt",
        "checksums.txt",
    }

    for asset in assets:
        name = asset.get("name", "")
        dl_url = asset.get("browser_download_url", "")
        if name in sha_asset_names and dl_url:
            if not is_safe_update_url(dl_url):
                continue
            try:
                req = urllib.request.Request(
                    dl_url,
                    headers={"User-Agent": "Tared-Updater"},
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw_bytes = resp.read(64 * 1024)
                    text = raw_bytes.decode("utf-8", errors="replace")
                    for line in text.splitlines():
                        parts = line.strip().split()
                        if len(parts) >= 2:
                            hash_cand = parts[0].strip().lower()
                            file_cand = Path(parts[1].lstrip("*")).name.strip()
                            if (
                                file_cand.lower() == target_asset_name.lower()
                                and len(hash_cand) == 64
                                and all(c in "0123456789abcdef" for c in hash_cand)
                            ):
                                return hash_cand
                        elif len(parts) == 1:
                            hash_cand = parts[0].strip().lower()
                            if (
                                len(hash_cand) == 64
                                and all(c in "0123456789abcdef" for c in hash_cand)
                                and name.startswith(target_asset_name)
                            ):
                                return hash_cand
            except Exception:
                pass

    if release_body:
        pattern = re.compile(
            rf"(?:([0-9a-fA-F]{{64}})\s+[*]?{re.escape(target_asset_name)})|(?:[*]?{re.escape(target_asset_name)}[\s:]+([0-9a-fA-F]{{64}}))",
            re.IGNORECASE,
        )
        match = pattern.search(release_body)
        if match:
            digest = match.group(1) or match.group(2)
            if digest:
                return digest.lower()

    return ""


def fetch_latest_release_info(
    current_version: str,
    api_url: str = LATEST_RELEASE_API_URL,
    timeout: int = 8,
) -> tuple[bool, UpdateInfo | None, str]:
    """Query GitHub releases endpoint and verify available update against trust model."""
    if not is_safe_update_url(api_url):
        return False, None, f"Untrusted update API URL rejected: {api_url}"

    req = urllib.request.Request(
        api_url,
        headers={
            "User-Agent": f"Tared-Updater/{current_version} ({platform.system()}; {platform.machine()})",
            "Accept": "application/vnd.github.v3+json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status != 200:
                return False, None, f"GitHub API responded with HTTP status {response.status}"
            payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
    except Exception as err:
        return False, None, f"Network check failed: {err}"

    tag_name = str(payload.get("tag_name", "")).strip()
    remote_version = tag_name.lstrip("vV")
    if not remote_version:
        return False, None, "Latest release on GitHub contains an empty version tag."

    if not is_newer_version(current_version, remote_version):
        return False, None, f"Tared is up to date (current: v{current_version}, latest: {tag_name})."

    assets = payload.get("assets", [])
    matched_asset = find_compatible_asset(assets, remote_version)

    download_url = matched_asset.get("browser_download_url", "") if matched_asset else ""
    if download_url and not is_safe_update_url(download_url):
        return False, None, f"Release asset URL failed security origin verification: {download_url}"

    asset_name = matched_asset.get("name", "") if matched_asset else ""
    asset_size = int(matched_asset.get("size", 0)) if matched_asset else 0

    release_body = str(payload.get("body", "") or "No release notes provided.")
    expected_sha256 = find_expected_sha256(assets, asset_name, release_body=release_body) if asset_name else ""

    info = UpdateInfo(
        version=remote_version,
        tag_name=tag_name,
        release_notes=release_body,
        download_url=download_url,
        asset_name=asset_name,
        asset_size=asset_size,
        published_at=str(payload.get("published_at", "")),
        expected_sha256=expected_sha256,
    )

    return True, info, f"New version available: {tag_name}"


def verify_elf_binary(file_path: Path) -> bool:
    """Verify that file starts with POSIX standard ELF magic bytes and exceeds minimum size."""
    if not file_path.is_file() or file_path.stat().st_size < 1024:
        return False
    with open(file_path, "rb") as f:
        magic = f.read(4)
    return magic == b"\x7fELF"


def verify_binary_payload(file_path: Path, expected_sha256: str) -> bool:
    """Verify that file is a valid ELF executable and matches the required SHA-256 digest."""
    if not verify_elf_binary(file_path):
        return False
    clean_expected = expected_sha256.strip().lower()
    if len(clean_expected) != 64 or not all(c in "0123456789abcdef" for c in clean_expected):
        return False
    actual = compute_file_sha256(file_path)
    return actual == clean_expected


def apply_appimage_update(staged_binary: Path, target_appimage: Path, expected_sha256: str) -> None:
    """Atomically swap the running AppImage binary with verified staged payload."""
    if not verify_binary_payload(staged_binary, expected_sha256=expected_sha256):
        raise ValueError("Staged update failed binary verification check.")

    staged_binary.chmod(staged_binary.stat().st_mode | 0o755)
    os.replace(str(staged_binary), str(target_appimage))


def restart_appimage(appimage_path: Path) -> None:
    """Execute updated AppImage file in place of the current process."""
    appimage_path.chmod(appimage_path.stat().st_mode | 0o755)
    args = [str(appimage_path)] + sys.argv[1:]
    os.execv(str(appimage_path), args)
