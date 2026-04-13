#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ -n "${SUDO_USER:-}" ]]; then
  APP_USER="$SUDO_USER"
else
  APP_USER="$USER"
fi

APP_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
if [[ -z "$APP_HOME" ]]; then
  echo "Could not determine home directory for user: $APP_USER"
  exit 1
fi

VENV_DIR="${VENV_DIR:-$APP_HOME/klippervault-venv}"
REQUIREMENTS_FILE="${REQUIREMENTS_FILE:-$APP_DIR/requirements.txt}"
KLIPPERVAULT_CONFIG_DIR="${KLIPPERVAULT_CONFIG_DIR:-$APP_HOME/.config/klippervault}"
KLIPPERVAULT_DB_PATH="${KLIPPERVAULT_DB_PATH:-$APP_HOME/.local/share/klippervault/klipper_macros.db}"

need_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Missing required command: $cmd"
    exit 1
  fi
}

as_root() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    echo "This step requires root privileges, but sudo is not available."
    exit 1
  fi
}

as_user() {
  if [[ "$(id -un)" == "$APP_USER" ]]; then
    "$@"
  else
    as_root runuser -u "$APP_USER" -- "$@"
  fi
}

echo "Installing KlipperVault (remote-only mode)"
echo "App dir: $APP_DIR"
echo "User: $APP_USER"
echo "Python: $PYTHON_BIN"
echo "Venv: $VENV_DIR"

after_install_cmd="$VENV_DIR/bin/python $APP_DIR/klipper_vault.py"

need_cmd "$PYTHON_BIN"
need_cmd getent

if [[ ! -f "$REQUIREMENTS_FILE" ]]; then
  echo "Requirements file not found: $REQUIREMENTS_FILE"
  exit 1
fi

as_user mkdir -p "$KLIPPERVAULT_CONFIG_DIR"
as_user mkdir -p "$(dirname "$KLIPPERVAULT_DB_PATH")"

if [[ ! -d "$VENV_DIR" ]]; then
  echo "Creating virtual environment..."
  as_user "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

echo "Installing Python dependencies..."
as_user "$VENV_DIR/bin/pip" install --upgrade pip
as_user "$VENV_DIR/bin/pip" install -r "$REQUIREMENTS_FILE"

echo "Settings are initialized automatically in the SQLite database on first start."

echo
echo "Install complete."
echo "Config dir: $KLIPPERVAULT_CONFIG_DIR"
echo "Database: $KLIPPERVAULT_DB_PATH"
echo "Start KlipperVault with: $after_install_cmd"
