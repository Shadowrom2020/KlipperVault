#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
KLIPPERVAULT_CONFIG_DIR="${KLIPPERVAULT_CONFIG_DIR:-$APP_HOME/.config/klippervault}"
KLIPPERVAULT_DB_PATH="${KLIPPERVAULT_DB_PATH:-$APP_HOME/.local/share/klippervault/klipper_macros.db}"
REMOVE_VENV="${REMOVE_VENV:-0}"
REMOVE_CONFIG="${REMOVE_CONFIG:-0}"
REMOVE_DB="${REMOVE_DB:-0}"

print_usage() {
  cat <<EOF
Usage: $(basename "$0") [--remove-venv] [--remove-config] [--remove-db]

Options:
  --remove-venv     Remove virtual environment at $VENV_DIR
  --remove-config   Remove config directory at $KLIPPERVAULT_CONFIG_DIR
  --remove-db       Remove database file at $KLIPPERVAULT_DB_PATH
EOF
}

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

for arg in "$@"; do
  case "$arg" in
    --remove-venv)
      REMOVE_VENV=1
      ;;
    --remove-config)
      REMOVE_CONFIG=1
      ;;
    --remove-db)
      REMOVE_DB=1
      ;;
    -h|--help)
      print_usage
      exit 0
      ;;
    *)
      echo "Unknown option: $arg"
      print_usage
      exit 1
      ;;
  esac
done

echo "Uninstalling KlipperVault (remote-only mode)"
echo "App dir: $APP_DIR"
echo "User: $APP_USER"

need_cmd getent

if [[ "$REMOVE_VENV" == "1" && -d "$VENV_DIR" ]]; then
  echo "Removing virtual environment: $VENV_DIR"
  as_user rm -rf "$VENV_DIR"
fi

if [[ "$REMOVE_CONFIG" == "1" && -d "$KLIPPERVAULT_CONFIG_DIR" ]]; then
  echo "Removing config directory: $KLIPPERVAULT_CONFIG_DIR"
  as_user rm -rf "$KLIPPERVAULT_CONFIG_DIR"
fi

if [[ "$REMOVE_DB" == "1" && -f "$KLIPPERVAULT_DB_PATH" ]]; then
  echo "Removing database: $KLIPPERVAULT_DB_PATH"
  as_user rm -f "$KLIPPERVAULT_DB_PATH"
fi

echo "Uninstall completed."
