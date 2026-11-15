r"""Automated release verification, Ed25519 authenticity, and AppImage update delivery for Tared."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
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

# Canonical repository public key for update verification (Ed25519 raw 32 bytes hex).
# Empty until a production signing key is provisioned; overridable via TARED_UPDATER_PUBLIC_KEY.
DEFAULT_UPDATER_PUBKEY_HEX = ""

# --- RFC 8032 Pure-Python Ed25519 Signature Verification ---
_ED25519_P = 2**255 - 19
_ED25519_L = 2**252 + 27742317777372353535851937790883648493
_ED25519_D = (-121665 * pow(121666, _ED25519_P - 2, _ED25519_P)) % _ED25519_P
_ED25519_I = pow(2, (_ED25519_P - 1) // 4, _ED25519_P)
_ED25519_BY = (4 * pow(5, _ED25519_P - 2, _ED25519_P)) % _ED25519_P


def _recover_x(y: int, sign: int) -> int:
    if y >= _ED25519_P:
        raise ValueError("y coordinate out of range")
    x2 = (y * y - 1) * pow(_ED25519_D * y * y + 1, _ED25519_P - 2, _ED25519_P) % _ED25519_P
    if x2 == 0:
        if sign:
            raise ValueError("Invalid x coordinate sign")
        return 0
    x = pow(x2, (_ED25519_P + 3) // 8, _ED25519_P)
    if (x * x - x2) % _ED25519_P != 0:
        x = (x * _ED25519_I) % _ED25519_P
    if (x * x - x2) % _ED25519_P != 0:
        raise ValueError("Cannot recover x coordinate")
    if (x & 1) != sign:
        x = _ED25519_P - x
    return x


_ED25519_BX = _recover_x(_ED25519_BY, 0)
_ED25519_B = (_ED25519_BX, _ED25519_BY)


def _ed_add(p1: tuple[int, int], p2: tuple[int, int]) -> tuple[int, int]:
    x1, y1 = p1
    x2, y2 = p2
    x3 = ((x1 * y2 + y1 * x2) * pow(1 + _ED25519_D * x1 * x2 * y1 * y2, _ED25519_P - 2, _ED25519_P)) % _ED25519_P
    y3 = ((y1 * y2 + x1 * x2) * pow(1 - _ED25519_D * x1 * x2 * y1 * y2, _ED25519_P - 2, _ED25519_P)) % _ED25519_P
    return x3, y3


def _ed_mul(pt: tuple[int, int], s: int) -> tuple[int, int]:
    res = (0, 1)
    base = pt
    while s > 0:
        if s & 1:
            res = _ed_add(res, base)
        base = _ed_add(base, base)
        s >>= 1
    return res


def _decode_point(pt_bytes: bytes) -> tuple[int, int]:
    if len(pt_bytes) != 32:
        raise ValueError("Invalid point encoding length")
    y = int.from_bytes(pt_bytes, "little")
    sign = (y >> 255) & 1
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return x, y


def verify_ed25519(pubkey_bytes: bytes, message: bytes, sig_bytes: bytes) -> bool:
    """Verify Ed25519 digital signature with RFC 8032 compliance and optional OpenSSL acceleration."""
    if len(pubkey_bytes) != 32 or len(sig_bytes) != 64:
        return False

    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519

        key = ed25519.Ed25519PublicKey.from_public_bytes(pubkey_bytes)
        key.verify(sig_bytes, message)
        return True
    except ImportError:
        pass
    except Exception:
        return False

    try:
        A = _decode_point(pubkey_bytes)
        r_bytes = sig_bytes[:32]
        R = _decode_point(r_bytes)
        s = int.from_bytes(sig_bytes[32:], "little")
        if s >= _ED25519_L:
            return False
        h = hashlib.sha512(r_bytes + pubkey_bytes + message).digest()
        k = int.from_bytes(h, "little") % _ED25519_L
        sb = _ed_mul(_ED25519_B, s)
        ka = _ed_mul(A, k)
        r_ka = _ed_add(R, ka)
        return sb == r_ka
    except Exception:
        return False


def parse_minisig_or_raw_signature(sig_text_or_bytes: str | bytes) -> bytes | None:
    """Extract raw 64-byte Ed25519 signature from minisign or base64 format."""
    if isinstance(sig_text_or_bytes, bytes):
        if len(sig_text_or_bytes) == 64:
            return sig_text_or_bytes
        text = sig_text_or_bytes.decode("utf-8", errors="replace")
    else:
        text = sig_text_or_bytes

    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    for line in lines:
        if line.startswith("untrusted comment") or line.startswith("trusted comment"):
            continue
        try:
            raw = base64.b64decode(line, validate=True)
            if len(raw) == 74:
                if raw[:2] == b"Ed":
                    return raw[10:74]
                return None
            if len(raw) == 64:
                return raw
        except ValueError:
            continue
    return None


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
    is_signature_verified: bool = False


def is_safe_update_url(url: str) -> bool:
    """Validate that update URL uses HTTPS and targets official GitHub domains."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ALLOWED_UPDATE_SCHEMES:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        return host in ALLOWED_UPDATE_HOSTS or any(
            host.endswith(f".{allowed}") for allowed in ("github.com", "githubusercontent.com")
        )
    except Exception:
        return False


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject HTTP redirects targeting untrusted schemes or hosts prior to connection."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        if not is_safe_update_url(newurl):
            raise urllib.error.URLError(f"Untrusted redirect destination rejected: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_STDLIB_URLOPEN = urllib.request.urlopen
_SAFE_OPENER = urllib.request.build_opener(_SafeRedirectHandler())


def open_safe_update_url(req: urllib.request.Request, timeout: int = 8) -> Any:
    """Open an update URL while enforcing origin safety on the initial request and all redirects."""
    if not is_safe_update_url(req.full_url):
        raise urllib.error.URLError(f"Untrusted update URL rejected: {req.full_url}")
    if urllib.request.urlopen is not _STDLIB_URLOPEN:
        return urllib.request.urlopen(req, timeout=timeout)
    return _SAFE_OPENER.open(req, timeout=timeout)


def _parse_prerelease_identifiers(pre_str: str) -> tuple[tuple[int, int | str], ...]:
    """Parse dot-separated SemVer prerelease identifiers into comparable tuples."""
    if not pre_str:
        return ()
    parts: list[tuple[int, int | str]] = []
    segments = [s for s in re.split(r"[.-]", pre_str) if s]
    for segment in segments:
        tokens = re.findall(r"[a-zA-Z]+|\d+", segment)
        for tok in tokens:
            if tok.isdigit():
                parts.append((0, int(tok)))
            else:
                parts.append((1, tok.lower()))
    return tuple(parts)


def _parse_semver_key(version_str: str) -> tuple[int, int, int, int, tuple[tuple[int, int | str], ...]]:
    """Parse a semantic version string into (major, minor, patch, is_release, pre_identifiers)."""
    clean = version_str.strip().lstrip("vV")
    if "+" in clean:
        clean = clean.split("+", 1)[0]

    is_release = 1
    pre_identifiers: tuple[tuple[int, int | str], ...] = ()
    if "-" in clean:
        clean, pre_str = clean.split("-", 1)
        is_release = 0
        pre_identifiers = _parse_prerelease_identifiers(pre_str)

    numbers: list[int] = []
    for segment in clean.split("."):
        try:
            numbers.append(int(segment))
        except ValueError:
            break

    while len(numbers) < 3:
        numbers.append(0)
    numbers = numbers[:3]

    return (numbers[0], numbers[1], numbers[2], is_release, pre_identifiers)


def parse_version(version_str: str) -> tuple[int, int, int, int, int]:
    """Parse a semantic version string into a comparable tuple: (major, minor, patch, is_release, pre_num)."""
    key = _parse_semver_key(version_str)
    pre_num = 0
    if key[4]:
        for tag_type, val in key[4]:
            if tag_type == 0 and isinstance(val, int):
                pre_num = val
                break
    return (key[0], key[1], key[2], key[3], pre_num)


def is_newer_version(current_ver: str, candidate_ver: str) -> bool:
    """Return True if candidate_ver is strictly newer than current_ver."""
    return _parse_semver_key(candidate_ver) > _parse_semver_key(current_ver)


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
    assets: list[dict[str, Any]],
    version: str,
    arch: str | None = None,
) -> dict[str, Any] | None:
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

    for asset in assets or []:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        if isinstance(name, str) and name and strict_pattern.match(name):
            return asset

    return None


def compute_file_sha256(file_path: Path) -> str:
    """Compute hex SHA-256 digest of a local file."""
    sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(64 * 1024):
            sha.update(chunk)
    return sha.hexdigest().lower()


def _fetch_asset_bytes(
    url: str,
    timeout: int = 6,
    cancel_check: Callable[[], bool] | None = None,
    on_response: Callable[[Any], None] | None = None,
) -> bytes | None:
    """Safely fetch companion asset text/bytes over verified HTTPS."""
    if cancel_check is not None and cancel_check():
        return None
    if not is_safe_update_url(url):
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Tared-Updater"})
        with open_safe_update_url(req, timeout=timeout) as resp:
            if on_response is not None:
                on_response(resp)
            try:
                if cancel_check is not None and cancel_check():
                    return None
                resp_url = getattr(resp, "geturl", lambda: None)()
                if isinstance(resp_url, str) and not is_safe_update_url(resp_url):
                    return None
                return resp.read(256 * 1024)
            finally:
                if on_response is not None:
                    on_response(None)
    except Exception:
        return None


def find_expected_sha256_and_signature(
    assets: list[dict[str, Any]],
    target_asset_name: str,
    release_body: str = "",
    pubkey_hex: str | None = None,
    timeout: int = 6,
    cancel_check: Callable[[], bool] | None = None,
    on_response: Callable[[Any], None] | None = None,
) -> tuple[str, bool]:
    """Resolve expected SHA-256 and verify detached Ed25519 signature of the release manifest."""
    resolved_pubkey = (
        (os.environ.get("TARED_UPDATER_PUBLIC_KEY") or DEFAULT_UPDATER_PUBKEY_HEX)
        if pubkey_hex is None
        else pubkey_hex
    )
    expected_hash = ""
    hash_from_manifest = False
    manifest_bytes: bytes | None = None

    manifest_pairs = [
        (
            (f"{target_asset_name}.sha256", f"{target_asset_name}.sha256.txt"),
            (f"{target_asset_name}.minisig", f"{target_asset_name}.sig"),
        ),
        (
            ("SHA256SUMS", "SHA256SUMS.txt", "checksums.txt"),
            ("SHA256SUMS.minisig", "SHA256SUMS.sig", "checksums.txt.sig", "checksums.txt.minisig"),
        ),
    ]

    assets_by_name = {asset.get("name", ""): asset for asset in assets if asset.get("name")}
    manifest_asset = None
    sig_asset = None

    # First pass: look for a manifest that has a matching signed companion
    for m_names, s_names in manifest_pairs:
        found_m = next((assets_by_name[n] for n in m_names if n in assets_by_name), None)
        found_s = next((assets_by_name[n] for n in s_names if n in assets_by_name), None)
        if found_m and found_s:
            manifest_asset = found_m
            sig_asset = found_s
            break

    # Second pass: fall back to unsigned manifest if no signed pair exists
    if manifest_asset is None:
        for m_names, _ in manifest_pairs:
            found_m = next((assets_by_name[n] for n in m_names if n in assets_by_name), None)
            if found_m:
                manifest_asset = found_m
                break

    fetch_kwargs: dict[str, Any] = {"timeout": timeout}
    if cancel_check is not None:
        fetch_kwargs["cancel_check"] = cancel_check
    if on_response is not None:
        fetch_kwargs["on_response"] = on_response

    if manifest_asset:
        if cancel_check is not None and cancel_check():
            return "", False
        raw_dl_url = manifest_asset.get("browser_download_url")
        dl_url = raw_dl_url if isinstance(raw_dl_url, str) else ""
        manifest_bytes = _fetch_asset_bytes(dl_url, **fetch_kwargs)
        if manifest_bytes:
            text = manifest_bytes.decode("utf-8", errors="replace")
            for line in text.splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) >= 2:
                    h_cand = parts[0].strip().lower()
                    f_cand = Path(parts[1].lstrip("*").strip()).name.strip()
                    if (
                        f_cand.lower() == target_asset_name.lower()
                        and len(h_cand) == 64
                        and all(c in "0123456789abcdef" for c in h_cand)
                    ):
                        expected_hash = h_cand
                        hash_from_manifest = True
                        break

    if not expected_hash and release_body:
        pattern = re.compile(
            rf"(?:([0-9a-fA-F]{{64}})\s+[*]?{re.escape(target_asset_name)})|"
            rf"(?:[*]?{re.escape(target_asset_name)}[\s:]+([0-9a-fA-F]{{64}}))",
            re.IGNORECASE,
        )
        match = pattern.search(release_body)
        if match:
            digest = match.group(1) or match.group(2)
            if digest:
                expected_hash = digest.lower()

    is_verified = False
    if sig_asset and manifest_bytes and hash_from_manifest and resolved_pubkey:
        if cancel_check is not None and cancel_check():
            return expected_hash, False
        raw_sig_url = sig_asset.get("browser_download_url")
        sig_url = raw_sig_url if isinstance(raw_sig_url, str) else ""
        raw_sig = _fetch_asset_bytes(sig_url, **fetch_kwargs)
        if raw_sig:
            parsed_sig = parse_minisig_or_raw_signature(raw_sig)
            if parsed_sig:
                try:
                    pk_bytes = bytes.fromhex(resolved_pubkey.strip())
                    is_verified = verify_ed25519(pk_bytes, manifest_bytes, parsed_sig)
                except Exception:
                    is_verified = False

    return expected_hash, is_verified


def find_expected_sha256(
    assets: list[dict[str, Any]],
    target_asset_name: str,
    release_body: str = "",
    timeout: int = 4,
) -> str:
    """Backward-compatible helper returning SHA-256 digest."""
    sha, _ = find_expected_sha256_and_signature(assets, target_asset_name, release_body=release_body, timeout=timeout)
    return sha


def fetch_latest_release_info(
    current_version: str,
    api_url: str = LATEST_RELEASE_API_URL,
    pubkey_hex: str | None = None,
    timeout: int = 8,
    cancel_check: Callable[[], bool] | None = None,
    on_response: Callable[[Any], None] | None = None,
) -> tuple[bool, UpdateInfo | None, str]:
    """Query GitHub releases endpoint and verify available update against authenticity model."""
    if cancel_check is not None and cancel_check():
        return False, None, "Update check cancelled."
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
        with open_safe_update_url(req, timeout=timeout) as response:
            if on_response is not None:
                on_response(response)
            try:
                if cancel_check is not None and cancel_check():
                    return False, None, "Update check cancelled."
                resp_url = getattr(response, "geturl", lambda: None)()
                if isinstance(resp_url, str) and not is_safe_update_url(resp_url):
                    return False, None, f"Untrusted redirect destination rejected: {resp_url}"
                if response.status != 200:
                    return False, None, f"GitHub API responded with HTTP status {response.status}"
                payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
            finally:
                if on_response is not None:
                    on_response(None)
    except Exception as err:
        return False, None, f"Network check failed: {err}"

    if cancel_check is not None and cancel_check():
        return False, None, "Update check cancelled."

    if not isinstance(payload, dict):
        return False, None, "Latest release metadata is not a JSON object."

    raw_tag = payload.get("tag_name")
    tag_name = raw_tag.strip() if isinstance(raw_tag, str) else ""
    remote_version = tag_name.lstrip("vV")
    if not remote_version:
        return False, None, "Latest release on GitHub contains an empty version tag."

    if not is_newer_version(current_version, remote_version):
        return False, None, f"Tared is up to date (current: v{current_version}, latest: {tag_name})."

    raw_assets = payload.get("assets")
    if raw_assets is None:
        assets: list[dict[str, Any]] = []
    elif not isinstance(raw_assets, list):
        return False, None, "Latest release metadata contains malformed assets list."
    else:
        assets = [asset for asset in raw_assets if isinstance(asset, dict)]
    matched_asset = find_compatible_asset(assets, remote_version)

    raw_dl_url = matched_asset.get("browser_download_url") if matched_asset else ""
    download_url = raw_dl_url if isinstance(raw_dl_url, str) else ""
    if download_url and not is_safe_update_url(download_url):
        return False, None, f"Release asset URL failed security origin verification: {download_url}"

    raw_asset_name = matched_asset.get("name") if matched_asset else ""
    asset_name = raw_asset_name if isinstance(raw_asset_name, str) else ""
    raw_asset_size = matched_asset.get("size") if matched_asset else 0
    if isinstance(raw_asset_size, bool) or raw_asset_size is None:
        asset_size = 0
    else:
        try:
            asset_size = max(0, int(raw_asset_size))
        except (TypeError, ValueError):
            asset_size = 0

    release_body = str(payload.get("body", "") or "No release notes provided.")
    sha_kwargs: dict[str, Any] = {
        "release_body": release_body,
        "pubkey_hex": pubkey_hex,
    }
    if cancel_check is not None:
        sha_kwargs["cancel_check"] = cancel_check
    if on_response is not None:
        sha_kwargs["on_response"] = on_response
    expected_sha256, is_verified = (
        find_expected_sha256_and_signature(assets, asset_name, **sha_kwargs)
        if asset_name
        else ("", False)
    )

    info = UpdateInfo(
        version=remote_version,
        tag_name=tag_name,
        release_notes=release_body,
        download_url=download_url,
        asset_name=asset_name,
        asset_size=asset_size,
        published_at=pub_at if isinstance((pub_at := payload.get("published_at")), str) else "",
        expected_sha256=expected_sha256,
        is_signature_verified=is_verified,
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


def apply_appimage_update(
    staged_binary: Path,
    target_appimage: Path,
    expected_sha256: str,
    new_asset_name: str = "",
) -> Path:
    """Atomically install verified staged payload, updating filename if versioned."""
    if not verify_binary_payload(staged_binary, expected_sha256=expected_sha256):
        raise ValueError("Staged update failed binary verification check.")

    staged_binary.chmod(staged_binary.stat().st_mode | 0o755)

    dest_path = target_appimage
    if new_asset_name and re.search(r"[-_]v?\d+\.\d+", target_appimage.name):
        safe_asset_name = Path(new_asset_name).name
        if (
            not safe_asset_name
            or safe_asset_name != new_asset_name
            or safe_asset_name in (".", "..")
            or "\\" in new_asset_name
        ):
            raise ValueError(f"Unsafe update asset filename rejected: {new_asset_name!r}")
        dest_path = target_appimage.parent / safe_asset_name

    os.replace(str(staged_binary), str(dest_path))

    if dest_path.resolve() != target_appimage.resolve() and target_appimage.exists():
        try:
            target_appimage.unlink()
        except OSError:
            pass

    return dest_path


def restart_appimage(appimage_path: Path) -> None:
    """Execute updated AppImage file in place of the current process, cleaning stale library paths."""
    appimage_path.chmod(appimage_path.stat().st_mode | 0o755)

    orig_ld = os.environ.get("TARED_ORIG_LD_LIBRARY_PATH")
    if orig_ld is not None:
        if orig_ld:
            os.environ["LD_LIBRARY_PATH"] = orig_ld
        else:
            os.environ.pop("LD_LIBRARY_PATH", None)
    else:
        appdir = os.environ.get("APPDIR", "")
        current_ld = os.environ.get("LD_LIBRARY_PATH", "")
        if current_ld and appdir:
            cleaned_parts = [p for p in current_ld.split(":") if p and not p.startswith(appdir)]
            if cleaned_parts:
                os.environ["LD_LIBRARY_PATH"] = ":".join(cleaned_parts)
            else:
                os.environ.pop("LD_LIBRARY_PATH", None)

    args = [str(appimage_path)] + sys.argv[1:]
    os.execv(str(appimage_path), args)
