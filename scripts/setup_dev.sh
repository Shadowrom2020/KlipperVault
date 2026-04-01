#!/usr/bin/env bash
# setup_dev.sh — Initialize the KlipperVault development environment
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$REPO_ROOT/.venv"
PYTHON="${PYTHON:-python3}"

echo "==> KlipperVault dev environment setup"
echo "    Repo root : $REPO_ROOT"
echo "    Virtualenv: $VENV_DIR"
echo "    Python    : $($PYTHON --version)"
echo ""

# --- Create virtualenv if missing ---
if [[ ! -d "$VENV_DIR" ]]; then
    echo "==> Creating virtual environment..."
    "$PYTHON" -m venv "$VENV_DIR"
else
    echo "==> Virtual environment already exists, skipping creation."
fi

# --- Activate ---
# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

# --- Upgrade pip silently ---
echo "==> Upgrading pip..."
pip install --quiet --upgrade pip

# --- Install dependencies ---
echo "==> Installing dependencies from requirements.txt..."
pip install --quiet -r "$REPO_ROOT/requirements.txt"

echo ""
echo "==> Done! Activate the environment with:"
echo "    source .venv/bin/activate"
echo ""
echo "==> Then launch the app with:"
echo "    python klipper_vault.py"
