#!/usr/bin/env python3
# Copyright (C) 2026 Juergen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared default filesystem paths for KlipperVault runtimes."""

from __future__ import annotations

import os
from pathlib import Path


def _runtime_mode() -> str:
	"""Resolve runtime mode from environment with sane fallback."""
	mode = str(os.environ.get("KLIPPERVAULT_RUNTIME_MODE", "auto")).strip().lower()
	if mode in {"auto", "on_printer", "off_printer"}:
		return mode
	return "auto"


def _default_config_dir() -> Path:
	"""Compute default config directory for the selected runtime mode."""
	override = str(os.environ.get("KLIPPERVAULT_CONFIG_DIR", "")).strip()
	if override:
		return Path(override).expanduser().resolve()

	mode = _runtime_mode()
	if mode == "off_printer":
		return (Path.home() / ".config" / "klippervault").resolve()

	# Keep printer_data default for on-printer and auto modes.
	return (Path.home() / "printer_data" / "config").resolve()


def _default_db_path() -> Path:
	"""Compute default database path for the selected runtime mode."""
	override = str(os.environ.get("KLIPPERVAULT_DB_PATH", "")).strip()
	if override:
		return Path(override).expanduser().resolve()

	mode = _runtime_mode()
	if mode == "off_printer":
		return (Path.home() / ".local" / "share" / "klippervault" / "klipper_macros.db").resolve()

	# Keep printer_data default for on-printer and auto modes.
	return (Path.home() / "printer_data" / "db" / "klipper_macros.db").resolve()


DEFAULT_CONFIG_DIR = str(_default_config_dir())
DEFAULT_DB_PATH = str(_default_db_path())
