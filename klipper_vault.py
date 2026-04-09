#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Root launcher for the KlipperVault NiceGUI app."""

from __future__ import annotations

import hashlib
import os
import subprocess  # nosec B404
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    # Keep imports stable after moving all source modules under src/.
    sys.path.insert(0, str(SRC_DIR))


def _requirements_hash(requirements_path: Path) -> str:
    """Return SHA256 hash of requirements file contents."""
    return hashlib.sha256(requirements_path.read_bytes()).hexdigest()


def _venv_requirements_stamp_path() -> Path:
    """Return per-venv stamp file used to skip redundant pip installs."""
    # Keep symlink path intact so venv python wrappers like
    # ~/.venv/bin/python -> /usr/bin/python still map back to ~/.venv.
    python_path = Path(sys.executable)
    if python_path.parent.name == "bin" and (python_path.parent.parent / "pyvenv.cfg").exists():
        return python_path.parent.parent / ".klippervault_requirements.sha256"
    return REPO_ROOT / ".klippervault_requirements.sha256"


def _auto_update_venv_enabled() -> bool:
    """Return True when startup venv sync is enabled."""
    raw = os.environ.get("KLIPPERVAULT_AUTO_UPDATE_VENV", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _log_venv_sync(message: str) -> None:
    """Emit a startup log line for venv sync decisions."""
    print(f"[KlipperVault] venv-sync: {message}", flush=True)


def _sync_venv_requirements_if_needed() -> None:
    """Install requirements into active venv when requirements.txt changed."""
    if not _auto_update_venv_enabled():
        _log_venv_sync("disabled via KLIPPERVAULT_AUTO_UPDATE_VENV")
        return

    requirements_path = REPO_ROOT / "requirements.txt"
    if not requirements_path.exists() or not requirements_path.is_file():
        _log_venv_sync("requirements.txt not found; skipping")
        return

    required_hash = _requirements_hash(requirements_path)
    stamp_path = _venv_requirements_stamp_path()

    try:
        installed_hash = stamp_path.read_text(encoding="utf-8").strip()
    except OSError:
        installed_hash = ""

    if installed_hash == required_hash:
        _log_venv_sync("requirements unchanged; skipping")
        return

    _log_venv_sync("requirements changed; running pip install")
    subprocess.run(  # nosec B603
        [sys.executable, "-m", "pip", "install", "-r", str(requirements_path)],
        cwd=str(REPO_ROOT),
        check=True,
    )

    stamp_path.write_text(required_hash + "\n", encoding="utf-8")
    _log_venv_sync("requirements sync completed")


def _load_app_version() -> str:
    """Read application version from VERSION file, with safe fallback."""
    version_path = REPO_ROOT / "VERSION"
    try:
        version = version_path.read_text(encoding="utf-8").strip()
        return version or "unknown"
    except OSError:
        return "unknown"


def main() -> None:
    """Start the NiceGUI application with configured runtime settings."""
    _sync_venv_requirements_if_needed()

    from klipper_macro_gui import DEFAULT_CONFIG_DIR, build_ui
    from klipper_vault_config import (
        ensure_moonraker_update_manager_managed_services as _ensure_moonraker_update_manager_managed_services,
    )
    from klipper_vault_config import load_or_create as _load_vault_config
    from klipper_vault_i18n import t
    from nicegui import ui

    config_dir = Path(DEFAULT_CONFIG_DIR).expanduser().resolve()
    vault_cfg = _load_vault_config(config_dir)
    _ensure_moonraker_update_manager_managed_services(config_dir)
    favicon_path = REPO_ROOT / "assets" / "favicon.svg"
    build_ui(app_version=_load_app_version())
    # Intentional: the web UI must be reachable from other devices on the LAN.
    ui.run(
        host="0.0.0.0",  # nosec B104
        port=vault_cfg.port,
        title=t("Klipper Vault"),
        dark=True,
        favicon=favicon_path,
        show=False,
        reload=False,
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()
