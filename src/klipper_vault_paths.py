#!/usr/bin/env python3
# Copyright (C) 2026 Juergen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared default filesystem paths for KlipperVault runtimes."""

from __future__ import annotations

import os
import platform
from pathlib import Path


def _runtime_mode() -> str:
    """Return fixed runtime mode (remote-only)."""
    return "off_printer"


def _platform_dirs() -> tuple[Path, Path]:
	"""Return platform-specific default config dir and DB path."""
	home = Path.home()
	system = platform.system().lower()

	if system == "windows":
		appdata = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
		localappdata = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
		config_dir = appdata / "KlipperVault"
		db_path = localappdata / "KlipperVault" / "klipper_macros.db"
		return config_dir.resolve(), db_path.resolve()

	if system == "darwin":
		support_dir = home / "Library" / "Application Support" / "KlipperVault"
		db_path = support_dir / "klipper_macros.db"
		return support_dir.resolve(), db_path.resolve()

	# Linux and other Unix-like systems follow XDG defaults.
	xdg_config_home = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
	xdg_data_home = Path(os.environ.get("XDG_DATA_HOME") or (home / ".local" / "share"))
	config_dir = xdg_config_home / "klippervault"
	db_path = xdg_data_home / "klippervault" / "klipper_macros.db"
	return config_dir.resolve(), db_path.resolve()


def _default_config_dir() -> Path:
	"""Compute default config directory for remote-only runtime."""
	config_dir, _ = _platform_dirs()
	return config_dir


def _default_db_path() -> Path:
	"""Compute default database path for remote-only runtime."""
	_, db_path = _platform_dirs()
	return db_path


DEFAULT_CONFIG_DIR = str(_default_config_dir())
DEFAULT_DB_PATH = str(_default_db_path())
