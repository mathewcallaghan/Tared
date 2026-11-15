#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}" || exit 1

STRICT=false
if [ "${1:-}" = "--strict" ]; then
    STRICT=true
fi

FAILURES=0
SKIPPED=0

run_check() {
    local label="$1"
    shift
    echo ""
    echo "=== ${label} ==="
    if "$@"; then
        echo "[PASS] ${label}"
    else
        echo "[FAIL] ${label}" >&2
        FAILURES=$((FAILURES + 1))
    fi
}

require_or_skip() {
    local bin="$1"
    local label="$2"
    local hint="$3"
    shift 3
    if command -v "${bin}" >/dev/null 2>&1; then
        run_check "${label}" "$@"
    else
        echo ""
        echo "=== ${label} ==="
        echo "[SKIP] '${bin}' not found (${hint})"
        SKIPPED=$((SKIPPED + 1))
        if [ "${STRICT}" = "true" ]; then
            FAILURES=$((FAILURES + 1))
        fi
    fi
}

run_check "Ruff (CI baseline)" ruff check .
run_check "Ruff (extended bug & security rules: src)" ruff check --extend-select B,DTZ,S,RUF006,RUF012,RUF015 --ignore B027,B905,S101,S110,S310,S606 src
run_check "Ruff (extended bug rules: tests)" ruff check --extend-select B,DTZ --ignore B017 tests

require_or_skip mypy "Static Type Check (mypy)" "pip install -e '.[audit]'" mypy src/tared
require_or_skip bandit "Security Scan (bandit)" "pip install -e '.[audit]'" bandit -r src/tared -ll -q

if command -v coverage >/dev/null 2>&1; then
    run_check "Test Suite + Branch Coverage" env QT_QPA_PLATFORM=offscreen coverage run -m unittest discover -s tests -p "test_*.py"
    run_check "Coverage Report" coverage report
else
    echo ""
    echo "=== Test Suite + Branch Coverage ==="
    echo "[WARN] 'coverage' not found (pip install -e '.[audit]'); running unittest without coverage"
    SKIPPED=$((SKIPPED + 1))
    if [ "${STRICT}" = "true" ]; then
        FAILURES=$((FAILURES + 1))
    fi
    run_check "Test Suite (unittest)" env QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -p "test_*.py"
fi

require_or_skip shellcheck "Shell Script Check (shellcheck)" "sudo apt-get install shellcheck" shellcheck scripts/build_appimage.sh scripts/audit.sh
require_or_skip pip-audit "Dependency Vulnerability Audit (pip-audit)" "pip install -e '.[audit]'" pip-audit

echo ""
echo "=== Audit Summary ==="
echo "Failures: ${FAILURES} | Skipped tools: ${SKIPPED}"
if [ "${FAILURES}" -gt 0 ]; then
    exit 1
fi
