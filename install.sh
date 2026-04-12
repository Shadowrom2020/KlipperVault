#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="klipper-vault.service"
HOST_API_SERVICE_NAME="klipper-vault-host-api.service"
PYTHON_BIN="${PYTHON_BIN:-python3}"
INSTALL_HOST_API_SERVICE="${INSTALL_HOST_API_SERVICE:-0}"
INSTALL_GUI_SERVICE="${INSTALL_GUI_SERVICE:-1}"
KLIPPERVAULT_RUNTIME_MODE="${KLIPPERVAULT_RUNTIME_MODE:-off_printer}"

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
LEGACY_VENV_DIR="${LEGACY_VENV_DIR:-$APP_HOME/klippervault-venv}"
REQUIREMENTS_FILE="${REQUIREMENTS_FILE:-$APP_DIR/requirements.txt}"
KLIPPERVAULT_CONFIG_DIR="${KLIPPERVAULT_CONFIG_DIR:-$APP_HOME/.config/klippervault}"
KLIPPERVAULT_DB_PATH="${KLIPPERVAULT_DB_PATH:-$APP_HOME/.local/share/klippervault/klipper_macros.db}"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"
HOST_API_SERVICE_PATH="/etc/systemd/system/${HOST_API_SERVICE_NAME}"
KLIPPER_CONFIG_DIR="${KLIPPER_CONFIG_DIR:-$APP_HOME/printer_data/config}"
VAULT_CFG_PATH="${VAULT_CFG_PATH:-$KLIPPER_CONFIG_DIR/klippervault.cfg}"
MAINSAIL_THEME_DIR="${MAINSAIL_THEME_DIR:-$KLIPPER_CONFIG_DIR/.theme}"
MAINSAIL_NAV_FILE="${MAINSAIL_NAV_FILE:-$MAINSAIL_THEME_DIR/navi.json}"
MOONRAKER_CONF_FILE="${MOONRAKER_CONF_FILE:-$KLIPPER_CONFIG_DIR/moonraker.conf}"
INSTALL_MAINSAIL_NAV="${INSTALL_MAINSAIL_NAV:-$INSTALL_GUI_SERVICE}"
MAINSAIL_NAV_TITLE="${MAINSAIL_NAV_TITLE:-KlipperVault}"
MAINSAIL_NAV_TARGET="${MAINSAIL_NAV_TARGET:-_blank}"
MAINSAIL_NAV_POSITION="${MAINSAIL_NAV_POSITION:-85}"
UPDATE_MANAGER_NAME="${UPDATE_MANAGER_NAME:-klippervault}"

LOW_MEMORY_THRESHOLD_KB=1048576

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

install_apt_dependencies() {
  if ! command -v apt-get >/dev/null 2>&1; then
    return
  fi

  echo "Detected apt-based system; installing required Python packages..."
  as_root apt-get update
  as_root apt-get install -y python3-venv python3-pip
}

check_system_memory() {
  if [[ ! -r /proc/meminfo ]]; then
    return
  fi

  local total_kb
  total_kb="$(awk '/^MemTotal:/ {print $2; exit}' /proc/meminfo)"

  if [[ ! "$total_kb" =~ ^[0-9]+$ ]]; then
    return
  fi

  if (( total_kb >= LOW_MEMORY_THRESHOLD_KB )); then
    return
  fi

  local total_mb
  total_mb=$((total_kb / 1024))

  echo "WARNING: Detected ${total_mb}MB RAM (less than 1024MB)."
  echo "KlipperVault may run poorly on systems with less than 1GB of RAM."

  if [[ ! -t 0 ]]; then
    echo "Cannot prompt for confirmation in non-interactive mode. Aborting install."
    exit 1
  fi

  if ! read -r -p "Continue anyway? [y/N]: " confirm_low_memory; then
    echo "Install canceled due to low memory."
    exit 1
  fi

  case "${confirm_low_memory,,}" in
    y|yes)
      ;;
    n|no|*)
      echo "Install canceled due to low memory."
      exit 1
      ;;
  esac
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

  local printer_ip
  printer_ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i == "src") {print $(i+1); exit}}')"

  if [[ -z "$printer_ip" ]]; then
    printer_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  fi

  if [[ -z "$printer_ip" ]]; then
    printer_ip="127.0.0.1"
  fi

  echo "http://${printer_ip}:${port}"
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

detect_repo_origin() {
  local repo_origin
  repo_origin="$(git -C "$APP_DIR" config --get remote.origin.url 2>/dev/null || true)"
  echo "$repo_origin"
}

detect_repo_branch() {
  local repo_branch
  repo_branch="$(git -C "$APP_DIR" branch --show-current 2>/dev/null || true)"
  if [[ -z "$repo_branch" ]]; then
    repo_branch="main"
  fi
  echo "$repo_branch"
}

setup_mainsail_update_section() {
  local repo_origin repo_branch
  local managed_services_csv=""
  local -a managed_services=()
  repo_origin="$(detect_repo_origin)"
  repo_branch="$(detect_repo_branch)"

  if [[ "$INSTALL_GUI_SERVICE" == "1" ]]; then
    managed_services+=("klipper-vault")
  fi
  if [[ "$INSTALL_HOST_API_SERVICE" == "1" ]]; then
    managed_services+=("klipper-vault-host-api")
  fi
  if [[ "${#managed_services[@]}" -eq 0 ]]; then
    managed_services+=("klipper-vault-host-api")
  fi
  managed_services_csv="$(IFS=,; echo "${managed_services[*]}")"

  echo "Configuring moonraker.conf update section..."
  as_user mkdir -p "$KLIPPER_CONFIG_DIR"

  as_user "$PYTHON_BIN" - "$MOONRAKER_CONF_FILE" "$UPDATE_MANAGER_NAME" "$APP_DIR" "$SERVICE_NAME" "$repo_origin" "$repo_branch" "$managed_services_csv" <<'PY'
import pathlib
import re
import sys

conf_path = pathlib.Path(sys.argv[1])
update_name = sys.argv[2]
app_dir = sys.argv[3]
_ = sys.argv[4]
repo_origin = sys.argv[5].strip()
repo_branch = sys.argv[6].strip() or "main"
managed_services_csv = sys.argv[7].strip()
managed_services = [service.strip() for service in managed_services_csv.split(",") if service.strip()]
if not managed_services:
  managed_services = ["klipper-vault-host-api"]

section_lines = [
    f"[update_manager {update_name}]",
    "type: git_repo",
    f"path: {app_dir}",
]

if repo_origin:
    section_lines.append(f"origin: {repo_origin}")

section_lines.extend([
    f"primary_branch: {repo_branch}",
  f"managed_services: {', '.join(managed_services)}",
])

section_text = "\n".join(section_lines) + "\n"

if conf_path.exists():
    content = conf_path.read_text(encoding="utf-8", errors="ignore")
else:
    content = ""

pattern = re.compile(
    rf"(?ms)^\[update_manager\s+{re.escape(update_name)}\]\n(?:.*?)(?=^\[|\Z)"
)

if pattern.search(content):
    updated = pattern.sub(section_text + "\n", content, count=1)
else:
    updated = content.rstrip("\n")
    if updated:
        updated += "\n\n"
    updated += section_text

conf_path.write_text(updated.rstrip("\n") + "\n", encoding="utf-8")
PY

  echo "Moonraker update section written: $MOONRAKER_CONF_FILE"
}

remove_mainsail_navigation_entry() {
  if [[ ! -f "$MAINSAIL_NAV_FILE" ]]; then
    return
  fi

  as_user "$PYTHON_BIN" - "$MAINSAIL_NAV_FILE" "$MAINSAIL_NAV_TITLE" <<'PY'
import json
import pathlib
import sys

nav_path = pathlib.Path(sys.argv[1])
title = sys.argv[2]

try:
    loaded = json.loads(nav_path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)

if not isinstance(loaded, list):
    raise SystemExit(0)

filtered = [item for item in loaded if not (isinstance(item, dict) and item.get("title") == title)]
if len(filtered) != len(loaded):
    nav_path.write_text(json.dumps(filtered, indent=2) + "\n", encoding="utf-8")
PY
}

migrate_legacy_installation() {
  if [[ "$VENV_DIR" != "$LEGACY_VENV_DIR" && ! -d "$VENV_DIR" && -d "$LEGACY_VENV_DIR" ]]; then
    echo "Migrating legacy virtualenv from $LEGACY_VENV_DIR to $VENV_DIR"
    as_root mv "$LEGACY_VENV_DIR" "$VENV_DIR"
    as_root chown -R "$APP_USER":"$APP_USER" "$VENV_DIR"
  fi

  if [[ "$INSTALL_HOST_API_SERVICE" != "1" ]]; then
    if as_root systemctl list-unit-files --type=service | grep -q "^${HOST_API_SERVICE_NAME}"; then
      echo "Disabling legacy host API service..."
      as_root systemctl disable --now "$HOST_API_SERVICE_NAME" || true
    fi
    if [[ -f "$HOST_API_SERVICE_PATH" ]]; then
      as_root rm -f "$HOST_API_SERVICE_PATH"
    fi
  fi

  if [[ "$INSTALL_GUI_SERVICE" != "1" ]]; then
    if as_root systemctl list-unit-files --type=service | grep -q "^${SERVICE_NAME}"; then
      echo "Disabling legacy GUI service for printer-only install..."
      as_root systemctl disable --now "$SERVICE_NAME" || true
    fi
    if [[ -f "$SERVICE_PATH" ]]; then
      as_root rm -f "$SERVICE_PATH"
    fi
    if [[ "$INSTALL_MAINSAIL_NAV" != "1" ]]; then
      remove_mainsail_navigation_entry
    fi
  fi
}

echo "Installing KlipperVault from: $APP_DIR"
echo "Service user: $APP_USER"
echo "Virtualenv: $VENV_DIR"
echo "Requirements file: $REQUIREMENTS_FILE"
echo "Install GUI service: $INSTALL_GUI_SERVICE"
echo "Install host API service: $INSTALL_HOST_API_SERVICE"
echo "Runtime mode: $KLIPPERVAULT_RUNTIME_MODE"
echo "KlipperVault config dir: $KLIPPERVAULT_CONFIG_DIR"
echo "KlipperVault DB path: $KLIPPERVAULT_DB_PATH"

need_cmd "$PYTHON_BIN"
need_cmd systemctl
need_cmd getent

check_system_memory

install_apt_dependencies

if [[ "$INSTALL_HOST_API_SERVICE" == "1" && ! -f "$APP_DIR/klipper_vault.py" ]]; then
  echo "klipper_vault.py not found in $APP_DIR"
  exit 1
fi

if [[ "$INSTALL_GUI_SERVICE" == "1" && ! -f "$APP_DIR/klipper_vault_gui.py" ]]; then
  echo "klipper_vault_gui.py not found in $APP_DIR"
  exit 1
fi

if [[ ! -f "$REQUIREMENTS_FILE" ]]; then
  echo "Requirements file not found: $REQUIREMENTS_FILE"
  exit 1
fi

migrate_legacy_installation

# Ensure python venv support is present.
if ! "$PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
  echo "python3 venv support is missing. Install python3-venv and retry."
  exit 1
fi

if [[ ! -d "$VENV_DIR" ]]; then
  echo "Creating virtual environment..."
  as_user "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# Ensure pip exists in the virtualenv even on distros that omit it by default.
as_user "$VENV_DIR/bin/python" -m ensurepip --upgrade

echo "Installing Python dependencies..."
as_user "$VENV_DIR/bin/python" -m pip install --upgrade pip
as_user "$VENV_DIR/bin/python" -m pip install -r "$REQUIREMENTS_FILE"

if [[ "$INSTALL_GUI_SERVICE" == "1" ]]; then
echo "Writing systemd service: $SERVICE_PATH"
TMP_SERVICE="$(mktemp)"
cat > "$TMP_SERVICE" <<EOF
[Unit]
Description=KlipperVault Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$VENV_DIR/bin/python $APP_DIR/klipper_vault_gui.py
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1
Environment=KLIPPERVAULT_AUTO_UPDATE_VENV=1
Environment=KLIPPERVAULT_REQUIREMENTS_FILE=$REQUIREMENTS_FILE
Environment=KLIPPERVAULT_RUNTIME_MODE=$KLIPPERVAULT_RUNTIME_MODE
Environment=KLIPPERVAULT_CONFIG_DIR=$KLIPPERVAULT_CONFIG_DIR
Environment=KLIPPERVAULT_DB_PATH=$KLIPPERVAULT_DB_PATH

[Install]
WantedBy=multi-user.target
EOF

as_root install -m 0644 "$TMP_SERVICE" "$SERVICE_PATH"
rm -f "$TMP_SERVICE"
fi

if [[ "$INSTALL_HOST_API_SERVICE" == "1" ]]; then
  echo "Writing host API systemd service: $HOST_API_SERVICE_PATH"
  TMP_HOST_API_SERVICE="$(mktemp)"
  cat > "$TMP_HOST_API_SERVICE" <<EOF
[Unit]
Description=KlipperVault Host API Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$VENV_DIR/bin/python $APP_DIR/klipper_vault.py
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1
Environment=KLIPPERVAULT_AUTO_UPDATE_VENV=1
Environment=KLIPPERVAULT_REQUIREMENTS_FILE=requirements-printer.txt

[Install]
WantedBy=multi-user.target
EOF

  as_root install -m 0644 "$TMP_HOST_API_SERVICE" "$HOST_API_SERVICE_PATH"
  rm -f "$TMP_HOST_API_SERVICE"
fi

echo "Reloading and enabling service..."
as_root systemctl daemon-reload
if [[ "$INSTALL_GUI_SERVICE" == "1" ]]; then
  as_root systemctl enable --now "$SERVICE_NAME"
fi
if [[ "$INSTALL_HOST_API_SERVICE" == "1" ]]; then
  as_root systemctl enable --now "$HOST_API_SERVICE_NAME"
fi

if [[ "$INSTALL_MAINSAIL_NAV" == "1" ]]; then
  setup_mainsail_navigation
fi
if [[ "$INSTALL_HOST_API_SERVICE" == "1" ]]; then
  setup_mainsail_update_section
else
  echo "Skipping moonraker update_manager integration (host API service not installed)."
fi

echo "Install complete."
if [[ "$INSTALL_GUI_SERVICE" == "1" ]]; then
  echo "Use: sudo systemctl restart $SERVICE_NAME"
fi
if [[ "$INSTALL_HOST_API_SERVICE" == "1" ]]; then
  echo "Use: sudo systemctl restart $HOST_API_SERVICE_NAME"
fi
if [[ "$INSTALL_GUI_SERVICE" == "1" ]]; then
  echo "Logs: sudo journalctl -u $SERVICE_NAME -f"
fi
if [[ "$INSTALL_HOST_API_SERVICE" == "1" ]]; then
  echo "Logs: sudo journalctl -u $HOST_API_SERVICE_NAME -f"
fi
