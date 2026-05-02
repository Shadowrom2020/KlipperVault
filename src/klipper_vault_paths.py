#!/usr/bin/env python3
# Copyright (C) 2026 Juergen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared default filesystem paths for KlipperVault runtimes."""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path


_WINDOWS_INSTALLER_MARKER = ".klippervault_installed"


def _runtime_mode() -> str:
    """Return fixed runtime mode (remote-only)."""
    return "standard"


def _is_frozen_runtime() -> bool:
	"""Return True when running from a packaged executable."""
	return bool(getattr(sys, "frozen", False))


def _windows_executable_dir() -> Path | None:
	"""Return executable directory for Windows frozen runtime, else None."""
	if platform.system().lower() != "windows" or not _is_frozen_runtime():
		return None
	try:
		return Path(sys.executable).resolve().parent
	except OSError:
		return None


def _is_windows_installer_runtime() -> bool:
	"""Return True when executable directory carries installer runtime marker."""
	exe_dir = _windows_executable_dir()
	if exe_dir is None:
		return False
	return (exe_dir / _WINDOWS_INSTALLER_MARKER).is_file()


def _platform_dirs() -> tuple[Path, Path]:
	"""Return platform-specific default config dir and DB path."""
	home = Path.home()
	system = platform.system().lower()

	if system == "windows":
		exe_dir = _windows_executable_dir()
		if exe_dir is not None and not _is_windows_installer_runtime():
			data_dir = exe_dir / "data"
			config_dir = data_dir / "config"
			db_path = data_dir / "klipper_macros.db"
			return config_dir.resolve(), db_path.resolve()

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
