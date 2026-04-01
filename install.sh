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
KLIPPER_CONFIG_DIR="${KLIPPER_CONFIG_DIR:-$APP_HOME/printer_data/config}"
VAULT_CFG_PATH="${VAULT_CFG_PATH:-$KLIPPER_CONFIG_DIR/klippervault.cfg}"
MAINSAIL_THEME_DIR="${MAINSAIL_THEME_DIR:-$KLIPPER_CONFIG_DIR/.theme}"
MAINSAIL_NAV_FILE="${MAINSAIL_NAV_FILE:-$MAINSAIL_THEME_DIR/navi.json}"
MAINSAIL_NAV_TITLE="${MAINSAIL_NAV_TITLE:-KlipperVault}"
MAINSAIL_NAV_TARGET="${MAINSAIL_NAV_TARGET:-_blank}"
MAINSAIL_NAV_POSITION="${MAINSAIL_NAV_POSITION:-85}"

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

detect_vault_port() {
  local default_port="10090"

  if [[ -f "$VAULT_CFG_PATH" ]]; then
    local parsed_port
    parsed_port="$(awk -F: '
      /^[[:space:]]*port[[:space:]]*:/ {
        gsub(/[[:space:]]/, "", $2)
        print $2
        exit
      }
    ' "$VAULT_CFG_PATH")"

    if [[ "$parsed_port" =~ ^[0-9]+$ ]] && (( parsed_port >= 1 && parsed_port <= 65535 )); then
      echo "$parsed_port"
      return
    fi
  fi

  echo "$default_port"
}

detect_nav_href() {
  local port="$1"

  if [[ -n "${MAINSAIL_NAV_HREF:-}" ]]; then
    echo "$MAINSAIL_NAV_HREF"
    return
  fi

  local printer_host
  printer_host="$(hostname -f 2>/dev/null || hostname)"
  echo "http://${printer_host}:${port}"
}

setup_mainsail_navigation() {
  local vault_port nav_href
  vault_port="$(detect_vault_port)"
  nav_href="$(detect_nav_href "$vault_port")"

  echo "Configuring Mainsail navigation entry..."
  as_user mkdir -p "$MAINSAIL_THEME_DIR"

  as_user "$PYTHON_BIN" - "$MAINSAIL_NAV_FILE" "$MAINSAIL_NAV_TITLE" "$nav_href" "$MAINSAIL_NAV_TARGET" "$MAINSAIL_NAV_POSITION" <<'PY'
import json
import pathlib
import sys

nav_path = pathlib.Path(sys.argv[1])
title = sys.argv[2]
href = sys.argv[3]
target = sys.argv[4]
position = int(sys.argv[5])

entry = {
    "title": title,
    "href": href,
    "target": target,
    "position": position,
    "icon": "M3,3H21V5H3V3M3,7H21V9H3V7M3,11H21V13H3V11M3,15H21V21H3V15M5,17V19H19V17H5Z",
}

data = []
if nav_path.exists():
    try:
        loaded = json.loads(nav_path.read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            data = loaded
    except Exception:
        data = []

updated = False
for idx, item in enumerate(data):
    if isinstance(item, dict) and item.get("title") == title:
        data[idx] = entry
        updated = True
        break

if not updated:
    data.append(entry)

nav_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PY

  echo "Mainsail nav entry written: $MAINSAIL_NAV_FILE"
  echo "KlipperVault URL: $nav_href"
}

echo "Installing KlipperVault from: $APP_DIR"
echo "Service user: $APP_USER"
echo "Virtualenv: $VENV_DIR"

need_cmd "$PYTHON_BIN"
need_cmd systemctl
need_cmd getent

if [[ ! -f "$APP_DIR/klipper_vault.py" ]]; then
  echo "klipper_vault.py not found in $APP_DIR"
  exit 1
fi

if [[ ! -f "$APP_DIR/requirements.txt" ]]; then
  echo "requirements.txt not found in $APP_DIR"
  exit 1
fi

# Ensure python venv support is present.
if ! "$PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
  echo "python3 venv support is missing. Install python3-venv and retry."
  exit 1
fi

if [[ ! -d "$VENV_DIR" ]]; then
  echo "Creating virtual environment..."
  as_user "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

echo "Installing Python dependencies..."
as_user "$VENV_DIR/bin/pip" install --upgrade pip
as_user "$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "Writing systemd service: $SERVICE_PATH"
TMP_SERVICE="$(mktemp)"
cat > "$TMP_SERVICE" <<EOF
[Unit]
Description=KlipperVault Service
After=network-online.target klipper.service mainsail.service
Wants=network-online.target klipper.service mainsail.service

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$VENV_DIR/bin/python $APP_DIR/klipper_vault.py
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

as_root install -m 0644 "$TMP_SERVICE" "$SERVICE_PATH"
rm -f "$TMP_SERVICE"

echo "Reloading and enabling service..."
as_root systemctl daemon-reload
as_root systemctl enable --now "$SERVICE_NAME"

setup_mainsail_navigation

# Display final service state for quick verification.
as_root systemctl --no-pager --full status "$SERVICE_NAME" || true

echo "Install complete."
echo "Use: sudo systemctl restart $SERVICE_NAME"
echo "Logs: sudo journalctl -u $SERVICE_NAME -f"
