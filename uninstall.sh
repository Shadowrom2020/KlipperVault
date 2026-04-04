#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="klipper-vault.service"
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
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"
REMOVE_VENV="${REMOVE_VENV:-0}"
KLIPPER_CONFIG_DIR="${KLIPPER_CONFIG_DIR:-$APP_HOME/printer_data/config}"
MOONRAKER_CONF_FILE="${MOONRAKER_CONF_FILE:-$KLIPPER_CONFIG_DIR/moonraker.conf}"
UPDATE_MANAGER_NAME="${UPDATE_MANAGER_NAME:-klippervault}"

print_usage() {
  cat <<EOF
Usage: $(basename "$0") [--remove-venv]

Options:
  --remove-venv   Remove the virtual environment at $VENV_DIR.

Environment overrides:
  PYTHON_BIN      Python interpreter for helper steps (default: $PYTHON_BIN)
  VENV_DIR        Path to the virtual environment (default: $VENV_DIR)
  REMOVE_VENV     Set to 1 to remove virtualenv without passing --remove-venv.
  KLIPPER_CONFIG_DIR Path to Klipper config dir (default: $KLIPPER_CONFIG_DIR)
  MOONRAKER_CONF_FILE Path to moonraker.conf (default: $MOONRAKER_CONF_FILE)
  UPDATE_MANAGER_NAME Update manager section name (default: $UPDATE_MANAGER_NAME)
EOF
}

for arg in "$@"; do
  case "$arg" in
    --remove-venv)
      REMOVE_VENV=1
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

need_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Missing required command: $cmd"
    exit 1
  fi
}

validate_venv_remove_target() {
  local target="$1"
  local app_home="$2"
  local resolved_target resolved_home

  resolved_target="$(readlink -f -- "$target" 2>/dev/null || true)"
  resolved_home="$(readlink -f -- "$app_home" 2>/dev/null || true)"

  if [[ -z "$resolved_target" || -z "$resolved_home" ]]; then
    echo "Refusing to remove virtualenv: could not resolve target path safely."
    return 1
  fi

  if [[ "$resolved_target" == "/" || "$resolved_target" == "$resolved_home" ]]; then
    echo "Refusing to remove virtualenv: unsafe target path: $resolved_target"
    return 1
  fi

  case "$resolved_target" in
    "$resolved_home"/*) ;;
    *)
      echo "Refusing to remove virtualenv outside user home: $resolved_target"
      return 1
      ;;
  esac

  if [[ "$(basename -- "$resolved_target")" != "klippervault-venv" ]]; then
    echo "Refusing to remove virtualenv with unexpected directory name: $resolved_target"
    return 1
  fi

  return 0
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

remove_moonraker_update_section() {
  if [[ ! -f "$MOONRAKER_CONF_FILE" ]]; then
    echo "Moonraker config not found; skipping update-manager cleanup."
    return
  fi

  echo "Removing Moonraker update section [update_manager $UPDATE_MANAGER_NAME]..."
  as_user "$PYTHON_BIN" - "$MOONRAKER_CONF_FILE" "$UPDATE_MANAGER_NAME" <<'PY'
import pathlib
import re
import sys

conf_path = pathlib.Path(sys.argv[1])
update_name = sys.argv[2]

content = conf_path.read_text(encoding="utf-8", errors="ignore")
pattern = re.compile(
    rf"(?ms)^\[update_manager\s+{re.escape(update_name)}\]\n(?:.*?)(?=^\[|\Z)"
)

updated, removed = pattern.subn("", content, count=1)
if removed:
    conf_path.write_text(updated.rstrip("\n") + "\n", encoding="utf-8")
    print("Removed update-manager section.")
else:
    print("No matching update-manager section found.")
PY
}

echo "Uninstalling KlipperVault from: $APP_DIR"
echo "Service user: $APP_USER"
echo "Service file: $SERVICE_PATH"
echo "Virtualenv: $VENV_DIR"

need_cmd systemctl
need_cmd getent
need_cmd readlink
need_cmd "$PYTHON_BIN"

if as_root systemctl list-unit-files --type=service | grep -q "^${SERVICE_NAME}"; then
  echo "Stopping service..."
  as_root systemctl stop "$SERVICE_NAME" || true

  echo "Disabling service..."
  as_root systemctl disable "$SERVICE_NAME" || true
else
  echo "Service unit $SERVICE_NAME is not registered; skipping stop/disable."
fi

if [[ -f "$SERVICE_PATH" ]]; then
  echo "Removing systemd service file..."
  as_root rm -f "$SERVICE_PATH"
else
  echo "Service file not found; skipping remove."
fi

echo "Reloading systemd daemon..."
as_root systemctl daemon-reload
as_root systemctl reset-failed || true

remove_moonraker_update_section

if [[ "$REMOVE_VENV" == "1" ]]; then
  if [[ -d "$VENV_DIR" ]]; then
    if ! validate_venv_remove_target "$VENV_DIR" "$APP_HOME"; then
      exit 1
    fi
    echo "Removing virtual environment..."
    as_root rm -rf "$VENV_DIR"
  else
    echo "Virtual environment not found; skipping remove."
  fi
else
  echo "Keeping virtual environment."
  echo "To remove it too, run: sudo ./uninstall.sh --remove-venv"
fi

echo "Uninstall complete."
