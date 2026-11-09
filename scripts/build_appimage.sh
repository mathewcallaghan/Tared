#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

APP_NAME="Tared"
VERSION=$(python3 -c "import re, pathlib; content = pathlib.Path('src/tared/__init__.py').read_text(); print(re.search(r'__version__\s*=\s*[\"\']([^\"\']+)[\"\']', content).group(1))")
ARCH="${ARCH:-x86_64}"
BUILD_DIR="${ROOT_DIR}/build"
APP_DIR="${BUILD_DIR}/AppDir"
OUT_APPIMAGE="${ROOT_DIR}/${APP_NAME}-${VERSION}-${ARCH}.AppImage"
PKG_ASSETS="${ROOT_DIR}/src/tared/assets"

echo "=== Building ${APP_NAME} v${VERSION} (${ARCH}) ==="

# 1. Resolve Python runner: isolate in virtualenv, reject host package pollution
if [ -n "${VIRTUAL_ENV:-}" ]; then
    PY_BIN="python3"
else
    BUILD_VENV="${BUILD_DIR}/.build_venv"
    if [ ! -d "${BUILD_VENV}" ]; then
        echo "--> Initializing build virtualenv (${BUILD_VENV})..."
        mkdir -p "${BUILD_DIR}"
        if ! python3 -m venv "${BUILD_VENV}" 2>/dev/null; then
            echo "ERROR: 'python3 -m venv' failed. Please install the virtualenv package for your system Python (e.g., 'sudo apt-get install python3-venv') to build in an isolated environment." >&2
            exit 1
        fi
    fi
    PY_BIN="${BUILD_VENV}/bin/python3"
fi

# 2. Ensure build dependencies exist in active runner
if ! "${PY_BIN}" -m PyInstaller --version >/dev/null 2>&1; then
    echo "--> Installing build tools into isolated environment..."
    "${PY_BIN}" -m pip install --quiet --upgrade pip
    "${PY_BIN}" -m pip install --quiet "pyinstaller>=6.10.0" "PyQt6>=6.5.0"
fi

# 3. Compile standalone application bundle with canonical import resolution
echo "--> Compiling application binary with PyInstaller..."
rm -rf dist "${APP_DIR}"

"${PY_BIN}" -m PyInstaller \
    --noconfirm \
    --clean \
    --name tared \
    --windowed \
    --paths "src" \
    --add-data "src/tared/printing/templates/batch_sheet.html:tared/printing/templates" \
    --add-data "src/tared/assets:tared/assets" \
    src/tared/__main__.py

# 4. Assemble AppDir
echo "--> Assembling AppDir structure..."
mkdir -p "${APP_DIR}/usr/bin"
mkdir -p "${APP_DIR}/usr/lib"
mkdir -p "${APP_DIR}/usr/share/applications"
mkdir -p "${APP_DIR}/usr/share/icons/hicolor/256x256/apps"
mkdir -p "${APP_DIR}/usr/share/icons/hicolor/scalable/apps"
mkdir -p "${APP_DIR}/assets"

cp -r dist/tared/* "${APP_DIR}/usr/bin/"
cp "${PKG_ASSETS}/tared.desktop" "${APP_DIR}/tared.desktop"
cp "${PKG_ASSETS}/tared.desktop" "${APP_DIR}/usr/share/applications/"
cp "${PKG_ASSETS}/tared.png" "${APP_DIR}/tared.png"
cp "${PKG_ASSETS}/tared.png" "${APP_DIR}/.DirIcon"
cp "${PKG_ASSETS}/tared.png" "${APP_DIR}/usr/share/icons/hicolor/256x256/apps/tared.png"
cp -r "${PKG_ASSETS}"/* "${APP_DIR}/assets/"

if [ -f "${PKG_ASSETS}/tared-icon.svg" ]; then
    cp "${PKG_ASSETS}/tared-icon.svg" "${APP_DIR}/tared-icon.svg"
    cp "${PKG_ASSETS}/tared-icon.svg" "${APP_DIR}/usr/share/icons/hicolor/scalable/apps/tared.svg"
fi

# AppRun launcher
cat << 'RUNNER' > "${APP_DIR}/AppRun"
#!/bin/sh
SELF=$(readlink -f "$0")
HERE=${SELF%/*}
export PATH="${HERE}/usr/bin:${PATH}"
export LD_LIBRARY_PATH="${HERE}/usr/lib:${LD_LIBRARY_PATH}"
exec "${HERE}/usr/bin/tared" "$@"
RUNNER
chmod +x "${APP_DIR}/AppRun"

# 5. Acquire appimagetool and zstd-capable runtime (pinned version default)
APPIMAGETOOL_TAG="${APPIMAGETOOL_TAG:-1.9.1}"
TOOL_PATH="${BUILD_DIR}/appimagetool"
RUNTIME_PATH="${BUILD_DIR}/runtime-${ARCH}"

mkdir -p "${BUILD_DIR}"

# Check ~/Downloads if user manually downloaded runtime
if [ ! -f "${RUNTIME_PATH}" ] || [ ! -s "${RUNTIME_PATH}" ]; then
    for dl_candidate in "${HOME}/Downloads/runtime-${ARCH}" "${HOME}/Downloads/runtime"; do
        if [ -f "${dl_candidate}" ] && [ -s "${dl_candidate}" ]; then
            echo "--> Found manual download at ${dl_candidate}, using for runtime..."
            cp "${dl_candidate}" "${RUNTIME_PATH}"
            chmod +x "${RUNTIME_PATH}"
            break
        fi
    done
fi

fetch_binary() {
    local target_path="$1"
    local url="$2"
    echo "--> Downloading from ${url}..."
    if command -v wget >/dev/null 2>&1; then
        if wget -q --tries=3 --timeout=15 -O "${target_path}.tmp" "${url}"; then
            mv "${target_path}.tmp" "${target_path}"
            chmod +x "${target_path}"
            return 0
        fi
    fi
    if curl --http1.1 -fsSL --retry 3 --retry-delay 2 --retry-connrefused \
        -A "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36" \
        -o "${target_path}.tmp" "${url}"; then
        mv "${target_path}.tmp" "${target_path}"
        chmod +x "${target_path}"
        return 0
    fi
    rm -f "${target_path}.tmp"
    return 1
}

if ! command -v appimagetool >/dev/null 2>&1; then
    if [ ! -f "${TOOL_PATH}" ] || [ ! -s "${TOOL_PATH}" ]; then
        if [ "${APPIMAGETOOL_TAG}" = "continuous" ]; then
            TOOL_URLS=(
                "${APPIMAGETOOL_URL:-}"
                "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage"
            )
        else
            TOOL_URLS=(
                "${APPIMAGETOOL_URL:-}"
                "https://github.com/AppImage/appimagetool/releases/download/${APPIMAGETOOL_TAG}/appimagetool-${ARCH}.AppImage"
            )
        fi
        DOWNLOADED=false
        for T_URL in "${TOOL_URLS[@]}"; do
            [ -z "${T_URL}" ] && continue
            if fetch_binary "${TOOL_PATH}" "${T_URL}"; then
                DOWNLOADED=true
                break
            fi
        done
        if [ "${DOWNLOADED}" != "true" ]; then
            echo "ERROR: Failed to download appimagetool. Place executable at ${TOOL_PATH}" >&2
            exit 1
        fi
        if [ -n "${APPIMAGETOOL_SHA256:-}" ]; then
            echo "--> Verifying appimagetool sha256 checksum..."
            echo "${APPIMAGETOOL_SHA256}  ${TOOL_PATH}" | sha256sum -c -
        fi
    fi
    APPIMAGETOOL="${TOOL_PATH}"
else
    APPIMAGETOOL="$(command -v appimagetool)"
fi

if [ ! -f "${RUNTIME_PATH}" ] || [ ! -s "${RUNTIME_PATH}" ]; then
    RUNTIME_URLS=(
        "${APPIMAGE_RUNTIME_URL:-}"
        "https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-${ARCH}"
    )
    DOWNLOADED_RUNTIME=false
    for R_URL in "${RUNTIME_URLS[@]}"; do
        [ -z "${R_URL}" ] && continue
        if fetch_binary "${RUNTIME_PATH}" "${R_URL}"; then
            DOWNLOADED_RUNTIME=true
            break
        fi
    done
    if [ "${DOWNLOADED_RUNTIME}" != "true" ]; then
        echo "ERROR: Failed to download zstd-compatible AppImage runtime." >&2
        exit 1
    fi
fi

if [ -n "${RUNTIME_SHA256:-}" ]; then
    echo "--> Verifying runtime sha256 checksum..."
    echo "${RUNTIME_SHA256}  ${RUNTIME_PATH}" | sha256sum -c -
fi

# 6. Build AppImage binary
echo "--> Generating ${OUT_APPIMAGE}..."
export ARCH="${ARCH}"
export APPIMAGE_EXTRACT_AND_RUN=1
export NO_APPSTREAM=1

"${APPIMAGETOOL}" --appimage-extract-and-run --no-appstream --runtime-file "${RUNTIME_PATH}" "${APP_DIR}" "${OUT_APPIMAGE}"

FINAL_SIZE=$(du -h "${OUT_APPIMAGE}" | cut -f1)
echo "=== Build Complete: ${OUT_APPIMAGE} (${FINAL_SIZE}) ==="
