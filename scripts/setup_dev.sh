#!/usr/bin/env bash
# setup_dev.sh — Initialize the KlipperVault development environment
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$REPO_ROOT/.venv"
PYENV_ROOT="${PYENV_ROOT:-$HOME/.pyenv}"
PYENV_PYTHON_MINOR="${PYENV_PYTHON_MINOR:-3.13}"
PYENV_PYTHON_VERSION="${PYENV_PYTHON_VERSION:-}"
INSTALL_SYSTEM_DEPS="${INSTALL_SYSTEM_DEPS:-1}"
INSTALL_DEV_TOOLS="${INSTALL_DEV_TOOLS:-1}"
INSTALL_PYENV="${INSTALL_PYENV:-1}"
TARGET_PYTHON_VERSION=""

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
        as_root apt-get install -y \
            build-essential curl git \
            libbz2-dev libffi-dev liblzma-dev libncursesw5-dev libreadline-dev \
            libsqlite3-dev libssl-dev tk-dev xz-utils zlib1g-dev
        return
    fi

    if have_cmd dnf; then
        as_root dnf install -y \
            gcc gcc-c++ make git curl patch \
            bzip2-devel libffi-devel openssl-devel readline-devel sqlite-devel xz-devel zlib-devel
        return
    fi

    if have_cmd pacman; then
        as_root pacman -Sy --noconfirm \
            base-devel git curl openssl zlib-ng-compat xz tk bzip2 libffi readline sqlite
        return
    fi

    if have_cmd zypper; then
        as_root zypper --non-interactive install \
            gcc gcc-c++ make git curl \
            libopenssl-devel readline-devel sqlite3-devel xz-devel zlib-devel libffi-devel
        return
    fi

    if have_cmd apk; then
        as_root apk add --no-cache \
            build-base bash curl git openssl-dev bzip2-dev zlib-dev xz-dev readline-dev sqlite-dev libffi-dev
        return
    fi

    echo "==> No supported package manager detected. Skipping system dependency installation."
}

ensure_pyenv_installed() {
    if have_cmd pyenv; then
        return
    fi

    if [[ "$INSTALL_PYENV" != "1" ]]; then
        echo "==> pyenv is required but missing (INSTALL_PYENV=$INSTALL_PYENV)."
        echo "    Install pyenv manually and re-run setup."
        exit 1
    fi

    echo "==> Installing pyenv into $PYENV_ROOT..."
    if [[ -d "$PYENV_ROOT/.git" ]]; then
        git -C "$PYENV_ROOT" pull --ff-only
    else
        rm -rf "$PYENV_ROOT"
        git clone https://github.com/pyenv/pyenv.git "$PYENV_ROOT"
    fi

    export PATH="$PYENV_ROOT/bin:$PATH"
}

activate_pyenv() {
    export PYENV_ROOT
    export PATH="$PYENV_ROOT/bin:$PATH"

    if ! have_cmd pyenv; then
        echo "==> pyenv command not found after installation attempt."
        exit 1
    fi

    # shellcheck disable=SC1091
    eval "$(pyenv init -)"
}

ensure_pyenv_python() {
    echo "==> Resolving latest Python ${PYENV_PYTHON_MINOR}.x via pyenv..."

    if [[ -n "$PYENV_PYTHON_VERSION" ]]; then
        TARGET_PYTHON_VERSION="$PYENV_PYTHON_VERSION"
    else
        TARGET_PYTHON_VERSION="$(
            pyenv install --list \
                | sed 's/^[[:space:]]*//' \
                | awk '/^3\.13\.[0-9]+$/ {print $0}' \
                | sort -V \
                | tail -n 1
        )"
    fi

    if [[ -z "$TARGET_PYTHON_VERSION" ]]; then
        echo "==> Could not resolve a Python ${PYENV_PYTHON_MINOR}.x version from pyenv."
        exit 1
    fi

    echo "==> Ensuring pyenv Python $TARGET_PYTHON_VERSION is installed..."
    pyenv install -s "$TARGET_PYTHON_VERSION"
}

venv_python_matches_target() {
    if [[ ! -x "$VENV_DIR/bin/python" ]]; then
        return 1
    fi

    local venv_version
    venv_version="$($VENV_DIR/bin/python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"
    [[ "$venv_version" == "$TARGET_PYTHON_VERSION" ]]
}

recreate_venv_if_needed() {
    if [[ ! -d "$VENV_DIR" ]]; then
        return
    fi

    if venv_python_matches_target; then
        return
    fi

    echo "==> Existing .venv is not Python $TARGET_PYTHON_VERSION; recreating..."
    rm -rf "$VENV_DIR"
}

setup_vscode_workspace_files() {
        local vscode_dir="$REPO_ROOT/.vscode"
        local settings_file="$vscode_dir/settings.json"
        local launch_file="$vscode_dir/launch.json"
        local env_file="$vscode_dir/.env"

        echo "==> Configuring VS Code workspace files..."
        mkdir -p "$vscode_dir"

        cat > "$env_file" <<ENV
PYTHONUNBUFFERED=1
PYTHONPATH=$REPO_ROOT:$REPO_ROOT/src

ENV
        echo "    Wrote .vscode/.env"

        cat > "$settings_file" <<'JSON'
{
    "python.defaultInterpreterPath": "${workspaceFolder}/.venv/bin/python",
    "python.terminal.activateEnvironment": true,
    "python.envFile": "${workspaceFolder}/.vscode/.env",
    "python.testing.pytestEnabled": true,
    "python.testing.pytestArgs": [
        "tests"
    ],
    "python.analysis.extraPaths": [
        "${workspaceFolder}",
        "${workspaceFolder}/src"
    ]
}
JSON
        echo "    Wrote .vscode/settings.json"

        cat > "$launch_file" <<'JSON'
{
    "version": "0.2.0",
    "configurations": [
        {
            "name": "Python: KlipperVault GUI (primary launcher)",
            "type": "python",
            "request": "launch",
            "python": "${config:python.defaultInterpreterPath}",
            "program": "${workspaceFolder}/klipper_vault_gui.py",
            "console": "integratedTerminal",
            "cwd": "${workspaceFolder}",
            "envFile": "${workspaceFolder}/.vscode/.env",
            "justMyCode": false,
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
echo "    pyenv root: $PYENV_ROOT"
if [[ -n "$PYENV_PYTHON_VERSION" ]]; then
    echo "    Python    : pyenv $PYENV_PYTHON_VERSION (manual override)"
else
    echo "    Python    : pyenv latest ${PYENV_PYTHON_MINOR}.x"
fi
echo ""

if [[ ! -f "$REPO_ROOT/requirements.txt" ]]; then
    echo "requirements.txt not found in $REPO_ROOT"
    exit 1
fi

if [[ ! -f "$REPO_ROOT/klipper_vault_gui.py" ]]; then
    echo "klipper_vault_gui.py not found in $REPO_ROOT"
    exit 1
fi

if [[ "$INSTALL_SYSTEM_DEPS" == "1" ]]; then
    install_system_dependencies
else
    echo "==> Skipping system dependency installation (INSTALL_SYSTEM_DEPS=$INSTALL_SYSTEM_DEPS)."
fi

ensure_pyenv_installed
activate_pyenv
ensure_pyenv_python

TARGET_PYTHON="$PYENV_ROOT/versions/$TARGET_PYTHON_VERSION/bin/python"
if [[ ! -x "$TARGET_PYTHON" ]]; then
    echo "==> Expected pyenv Python not found: $TARGET_PYTHON"
    exit 1
fi

echo "    Using interpreter: $($TARGET_PYTHON --version)"

recreate_venv_if_needed

# --- Create virtualenv if missing ---
if [[ ! -d "$VENV_DIR" ]]; then
    echo "==> Creating virtual environment..."
    "$TARGET_PYTHON" -m venv "$VENV_DIR"
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

# --- Ensure bundling toolchain ---
echo "==> Installing PyInstaller (required for bundle builds)..."
"$VENV_DIR/bin/python" -m pip install --quiet pyinstaller

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
echo "==> Debug defaults are in .vscode/.env"
echo "==> VS Code launch target for deep debugging:"
echo "    Python: KlipperVault GUI (primary launcher)"
echo ""
echo "==> Then launch the app with:"
echo "    python klipper_vault_gui.py"
echo ""
echo "==> To skip system dependencies next run:"
echo "    INSTALL_SYSTEM_DEPS=0 ./scripts/setup_dev.sh"
