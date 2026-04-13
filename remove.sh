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

DRY_RUN=0
REMOVE_SHORTCUTS=1

print_usage() {
  cat <<EOF
Usage: $(basename "$0") [--dry-run] [--keep-shortcuts]

Removes legacy on-printer KlipperVault installations and artifacts.

Options:
  --dry-run         Print actions without applying changes
  --keep-shortcuts  Keep legacy launcher files in home directory
  -h, --help        Show this help
EOF
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

run_cmd() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] $*"
    return 0
  fi
  "$@"
}

for arg in "$@"; do
  case "$arg" in
    --dry-run)
      DRY_RUN=1
      ;;
    --keep-shortcuts)
      REMOVE_SHORTCUTS=0
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

echo "Removing legacy on-printer KlipperVault installation"
echo "App dir: $APP_DIR"
echo "Target user: $APP_USER"
echo "Target home: $APP_HOME"

echo
echo "1) Stopping/disabling legacy services"
for svc in klippervault.service klipper-vault.service; do
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] sudo systemctl stop $svc"
    echo "[dry-run] sudo systemctl disable $svc"
  else
    as_root systemctl stop "$svc" >/dev/null 2>&1 || true
    as_root systemctl disable "$svc" >/dev/null 2>&1 || true
  fi
done

echo
echo "2) Removing legacy service unit files"
for svc_file in /etc/systemd/system/klippervault.service /etc/systemd/system/klipper-vault.service; do
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] sudo rm -f $svc_file"
  else
    as_root rm -f "$svc_file"
  fi
done

if [[ "$DRY_RUN" == "1" ]]; then
  echo "[dry-run] sudo systemctl daemon-reload"
else
  as_root systemctl daemon-reload
fi

echo
echo "3) Removing old app directories and virtual environments"
for dir_path in \
  "$APP_HOME/KlipperVault" \
  "$APP_HOME/klippervault-venv" \
  "$APP_HOME/printer_data/klippervault-venv"; do
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] rm -rf $dir_path"
  else
    as_user rm -rf "$dir_path"
  fi
done

echo
echo "4) Removing legacy runtime/config artifacts"
for file_path in \
  "$APP_HOME/printer_data/config/klippervault.cfg" \
  "$APP_HOME/printer_data/database/klippervault.db" \
  "$APP_HOME/printer_data/database/klipper_macros.db"; do
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] rm -f $file_path"
  else
    as_user rm -f "$file_path"
  fi
done

for dir_path in \
  "$APP_HOME/.config/klippervault" \
  "$APP_HOME/.local/share/klippervault"; do
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] rm -rf $dir_path"
  else
    as_user rm -rf "$dir_path"
  fi
done

if [[ "$REMOVE_SHORTCUTS" == "1" ]]; then
  echo
  echo "5) Removing legacy launcher shortcuts"
  for file_path in "$APP_HOME/klipper_vault.py" "$APP_HOME/klipper_vault_gui.py"; do
    if [[ "$DRY_RUN" == "1" ]]; then
      echo "[dry-run] rm -f $file_path"
    else
      as_user rm -f "$file_path"
    fi
  done
fi

echo
echo "6) Removing legacy Mainsail integration link(s)"
for mainsail_link in \
  "$APP_HOME/printer_data/www/klippervault" \
  "$APP_HOME/printer_data/www/klipper-vault" \
  "$APP_HOME/printer_data/config/mainsail/klippervault" \
  "$APP_HOME/printer_data/config/mainsail/klipper-vault" \
  "$APP_HOME/printer_data/config/.mainsail/klippervault" \
  "$APP_HOME/printer_data/config/.mainsail/klipper-vault"; do
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] rm -f $mainsail_link"
  else
    # Only remove links/files with explicit legacy KlipperVault names.
    as_user rm -f "$mainsail_link"
  fi
done

echo
echo "7) Verification"
for svc_file in /etc/systemd/system/klippervault.service /etc/systemd/system/klipper-vault.service; do
  if [[ -e "$svc_file" ]]; then
    echo "- Remaining service file: $svc_file"
  fi
done

for path_to_check in \
  "$APP_HOME/KlipperVault" \
  "$APP_HOME/klippervault-venv" \
  "$APP_HOME/printer_data/klippervault-venv" \
  "$APP_HOME/printer_data/config/klippervault.cfg" \
  "$APP_HOME/printer_data/database/klippervault.db" \
  "$APP_HOME/printer_data/database/klipper_macros.db" \
  "$APP_HOME/.config/klippervault" \
  "$APP_HOME/.local/share/klippervault" \
  "$APP_HOME/printer_data/www/klippervault" \
  "$APP_HOME/printer_data/www/klipper-vault" \
  "$APP_HOME/printer_data/config/mainsail/klippervault" \
  "$APP_HOME/printer_data/config/mainsail/klipper-vault" \
  "$APP_HOME/printer_data/config/.mainsail/klippervault" \
  "$APP_HOME/printer_data/config/.mainsail/klipper-vault"; do
  if [[ -e "$path_to_check" ]]; then
    echo "- Remaining path: $path_to_check"
  fi
done

if [[ "$REMOVE_SHORTCUTS" == "1" ]]; then
  for path_to_check in "$APP_HOME/klipper_vault.py" "$APP_HOME/klipper_vault_gui.py"; do
    if [[ -e "$path_to_check" ]]; then
      echo "- Remaining path: $path_to_check"
    fi
  done
fi

echo
if [[ "$DRY_RUN" == "1" ]]; then
  echo "Dry run complete. No changes were made."
else
  echo "Legacy on-printer removal completed."
fi
