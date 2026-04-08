#!/usr/bin/env bash
# setup_dev.sh — Initialize the KlipperVault development environment
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$REPO_ROOT/.venv"
PYTHON="${PYTHON:-python3}"
INSTALL_SYSTEM_DEPS="${INSTALL_SYSTEM_DEPS:-1}"
INSTALL_DEV_TOOLS="${INSTALL_DEV_TOOLS:-1}"

have_cmd() {
    command -v "$1" >/dev/null 2>&1
}

as_root() {
    if [[ "$(id -u)" -eq 0 ]]; then
        "$@"
    elif have_cmd sudo; then
        sudo "$@"
    else
        echo "==> Root privileges required for system package installation, but sudo is not available."
        return 1
    fi
}

install_system_dependencies() {
    echo "==> Installing system dependencies..."

    if have_cmd apt-get; then
        as_root apt-get update
        as_root apt-get install -y python3-venv python3-pip python3-dev build-essential git
        return
    fi

    if have_cmd dnf; then
        as_root dnf install -y python3-pip python3-devel gcc gcc-c++ make git
        return
    fi

    if have_cmd pacman; then
        as_root pacman -Sy --noconfirm python python-pip base-devel git
        return
    fi

    if have_cmd zypper; then
        as_root zypper --non-interactive install python3-pip python3-devel gcc gcc-c++ make git
        return
    fi

    if have_cmd apk; then
        as_root apk add --no-cache python3 py3-pip python3-dev build-base git
        return
    fi

    echo "==> No supported package manager detected. Skipping system dependency installation."
}

setup_vscode_workspace_files() {
        local vscode_dir="$REPO_ROOT/.vscode"
        local settings_file="$vscode_dir/settings.json"
        local launch_file="$vscode_dir/launch.json"

        echo "==> Configuring VS Code workspace files..."
        mkdir -p "$vscode_dir"

        cat > "$settings_file" <<'JSON'
{
    "python.defaultInterpreterPath": "${workspaceFolder}/.venv/bin/python",
    "python.terminal.activateEnvironment": true,
    "python.analysis.extraPaths": [
        "${workspaceFolder}"
    ]
}
JSON
        echo "    Wrote .vscode/settings.json"

        cat > "$launch_file" <<'JSON'
{
    "version": "0.2.0",
    "configurations": [
        {
            "name": "Python: NiceGUI App (configured port)",
            "type": "python",
            "request": "launch",
            "python": "${config:python.defaultInterpreterPath}",
            "program": "${workspaceFolder}/klipper_vault.py",
            "console": "integratedTerminal",
            "cwd": "${workspaceFolder}",
            "justMyCode": true,
            "env": {
                "PYTHONUNBUFFERED": "1"
            }
        },
        {
            "name": "Python: Index Macros (CLI)",
            "type": "python",
            "request": "launch",
            "python": "${config:python.defaultInterpreterPath}",
            "program": "${workspaceFolder}/src/klipper_macro_indexer.py",
            "console": "integratedTerminal",
            "cwd": "${workspaceFolder}",
            "justMyCode": true,
            "args": [
                "--config-dir",
                "${env:HOME}/printer_data/config",
                "--db-path",
                "${env:HOME}/printer_data/db/klipper_macros.db"
            ],
            "env": {
                "PYTHONUNBUFFERED": "1"
            }
        },
        {
            "name": "Python: Index Macros (CLI, prune stale)",
            "type": "python",
            "request": "launch",
            "python": "${config:python.defaultInterpreterPath}",
            "program": "${workspaceFolder}/src/klipper_macro_indexer.py",
            "console": "integratedTerminal",
            "cwd": "${workspaceFolder}",
            "justMyCode": true,
            "args": [
                "--config-dir",
                "${env:HOME}/printer_data/config",
                "--db-path",
                "${env:HOME}/printer_data/db/klipper_macros.db",
                "--prune"
            ],
            "env": {
                "PYTHONUNBUFFERED": "1"
            }
        }
    ]
}
JSON
        echo "    Wrote .vscode/launch.json"
}

echo "==> KlipperVault dev environment setup"
echo "    Repo root : $REPO_ROOT"
echo "    Virtualenv: $VENV_DIR"
if ! have_cmd "$PYTHON"; then
    echo "Python executable not found: $PYTHON"
    exit 1
fi

echo "    Python    : $($PYTHON --version)"
echo ""

if [[ ! -f "$REPO_ROOT/requirements.txt" ]]; then
    echo "requirements.txt not found in $REPO_ROOT"
    exit 1
fi

if [[ "$INSTALL_SYSTEM_DEPS" == "1" ]]; then
    install_system_dependencies
else
    echo "==> Skipping system dependency installation (INSTALL_SYSTEM_DEPS=$INSTALL_SYSTEM_DEPS)."
fi

if ! "$PYTHON" -m venv --help >/dev/null 2>&1; then
    echo "Python venv support is missing. Install your distro's python venv package and retry."
    exit 1
fi

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
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip

# --- Install dependencies ---
echo "==> Installing dependencies from requirements.txt..."
"$VENV_DIR/bin/python" -m pip install --quiet -r "$REPO_ROOT/requirements.txt"

# --- Optional dev/test tooling ---
if [[ -f "$REPO_ROOT/requirements-dev.txt" ]]; then
    echo "==> Installing dependencies from requirements-dev.txt..."
    "$VENV_DIR/bin/python" -m pip install --quiet -r "$REPO_ROOT/requirements-dev.txt"
elif [[ "$INSTALL_DEV_TOOLS" == "1" ]]; then
    echo "==> Installing common dev tooling (pytest, pytest-cov, ruff, black)..."
    "$VENV_DIR/bin/python" -m pip install --quiet pytest pytest-cov ruff black
else
    echo "==> Skipping dev tooling install (INSTALL_DEV_TOOLS=$INSTALL_DEV_TOOLS)."
fi

if [[ -n "${PIP_EXTRA_PACKAGES:-}" ]]; then
    echo "==> Installing extra Python packages from PIP_EXTRA_PACKAGES..."
    # Split on whitespace into an array intentionally.
    read -r -a extra_packages <<<"$PIP_EXTRA_PACKAGES"
    "$VENV_DIR/bin/python" -m pip install --quiet "${extra_packages[@]}"
fi

setup_vscode_workspace_files

echo ""
echo "==> Done! Development environment initialized."
echo "==> Activate the environment with:"
echo "    source .venv/bin/activate"
echo ""
echo "==> Then launch the app with:"
echo "    python klipper_vault.py"
echo ""
echo "==> To skip system dependencies next run:"
echo "    INSTALL_SYSTEM_DEPS=0 ./scripts/setup_dev.sh"
