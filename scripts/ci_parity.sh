#!/usr/bin/env bash
# ci_parity.sh — Run local checks equivalent to GitHub CI workflows.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
RUN_QUALITY=1
RUN_SECURITY=1
RUN_WORKFLOWS=1
STRICT_MYPY="${STRICT_MYPY:-0}"

have_cmd() {
    command -v "$1" >/dev/null 2>&1
}

usage() {
    cat <<'EOF'
Usage: ./scripts/ci_parity.sh [--quality|--security|--workflows|--all]

Optional:
    --workflows  Run GitHub Actions workflow lint parity (actionlint)

Environment:
    STRICT_MYPY=1  Fail quality checks when mypy reports errors (default: 0)

Runs CI-parity checks locally using the project virtualenv.
Defaults to --all.
EOF
}

for arg in "$@"; do
    case "$arg" in
        --quality)
            RUN_QUALITY=1
            RUN_SECURITY=0
            RUN_WORKFLOWS=0
            ;;
        --security)
            RUN_QUALITY=0
            RUN_SECURITY=1
            RUN_WORKFLOWS=0
            ;;
        --workflows)
            RUN_QUALITY=0
            RUN_SECURITY=0
            RUN_WORKFLOWS=1
            ;;
        --all)
            RUN_QUALITY=1
            RUN_SECURITY=1
            RUN_WORKFLOWS=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg"
            usage
            exit 1
            ;;
    esac
done

cd "$REPO_ROOT"

if [[ "$RUN_QUALITY" == "1" || "$RUN_SECURITY" == "1" ]]; then
    if [[ "$PYTHON_BIN" == */* ]]; then
        if [[ ! -x "$PYTHON_BIN" ]]; then
            echo "Python interpreter not found: $PYTHON_BIN"
            echo "Run ./scripts/setup_dev.sh first."
            exit 1
        fi
    else
        if ! have_cmd "$PYTHON_BIN"; then
            echo "Python interpreter not found: $PYTHON_BIN"
            echo "Run ./scripts/setup_dev.sh first."
            exit 1
        fi
        PYTHON_BIN="$(command -v "$PYTHON_BIN")"
    fi

    if [[ ! -f "$REPO_ROOT/requirements.txt" ]]; then
        echo "requirements.txt not found in $REPO_ROOT"
        exit 1
    fi

    echo "==> Using Python: $($PYTHON_BIN --version)"

    echo "==> Synchronizing tool dependencies"
    "$PYTHON_BIN" -m pip install --upgrade pip
    "$PYTHON_BIN" -m pip install -r requirements.txt
    "$PYTHON_BIN" -m pip install ruff mypy pytest packaging pip-audit bandit
fi

if [[ "$RUN_QUALITY" == "1" ]]; then
    echo "==> Running PR quality parity checks"
    "$PYTHON_BIN" -m ruff check .
    if PYTHONPATH=src "$PYTHON_BIN" -m mypy src klipper_vault_gui.py --ignore-missing-imports; then
        echo "mypy check passed."
    else
        if [[ "$STRICT_MYPY" == "1" ]]; then
            echo "mypy check failed and STRICT_MYPY=1 is set."
            exit 1
        fi
        echo "Warning: mypy check reported issues (non-blocking; set STRICT_MYPY=1 to enforce)."
    fi
    "$PYTHON_BIN" -m py_compile klipper_vault_gui.py src/*.py

    "$PYTHON_BIN" -m babel.messages.frontend extract -F babel.ini -o /tmp/fresh.pot src klipper_vault_gui.py
    "$PYTHON_BIN" - <<'PY'
import sys
from babel.messages.pofile import read_po

def msgids(path):
    with open(path, "rb") as f:
        return {m.id for m in read_po(f) if m.id}

committed = msgids("src/locales/klippervault.pot")
fresh = msgids("/tmp/fresh.pot")

added = fresh - committed
removed = committed - fresh

if added:
    print("New translatable strings not in committed .pot:")
    for s in sorted(added):
        print(f"  + {s!r}")
if removed:
    print("Strings removed from source but still in committed .pot:")
    for s in sorted(removed):
        print(f"  - {s!r}")
if added or removed:
    print("Run 'make i18n' and commit the updated catalog files.")
    sys.exit(1)

print("i18n catalog is up to date.")
PY

    "$PYTHON_BIN" scripts/check_gplv3_compatibility.py
    PYTHONPATH=src "$PYTHON_BIN" -m pytest -q
fi

if [[ "$RUN_SECURITY" == "1" ]]; then
    echo "==> Running security parity checks"
    "$PYTHON_BIN" -m pip_audit
    "$PYTHON_BIN" -m bandit -q -r src klipper_vault_gui.py -s B608
fi

if [[ "$RUN_WORKFLOWS" == "1" ]]; then
    echo "==> Running workflow lint parity checks"
    if have_cmd actionlint; then
        actionlint
    elif have_cmd docker; then
        docker run --rm -v "$PWD":/repo -w /repo rhysd/actionlint:latest
    else
        echo "Neither 'actionlint' nor 'docker' is available to run workflow lint parity checks."
        echo "Install actionlint or Docker, or run with --quality/--security only."
        exit 1
    fi
fi

echo "==> CI parity checks completed successfully"
