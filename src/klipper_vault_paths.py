#!/usr/bin/env python3
# Copyright (C) 2026 Juergen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared default filesystem paths for KlipperVault runtimes."""

from __future__ import annotations

import os
from pathlib import Path


def _runtime_mode() -> str:
	"""Resolve runtime mode from environment with remote-only fallback."""
	mode = str(os.environ.get("KLIPPERVAULT_RUNTIME_MODE", "off_printer")).strip().lower()
	if mode == "off_printer":
		return mode
	return "off_printer"


def _default_config_dir() -> Path:
	"""Compute default config directory for remote-only runtime."""
	override = str(os.environ.get("KLIPPERVAULT_CONFIG_DIR", "")).strip()
	if override:
		return Path(override).expanduser().resolve()
	return (Path.home() / ".config" / "klippervault").resolve()


def _default_db_path() -> Path:
	"""Compute default database path for remote-only runtime."""
	override = str(os.environ.get("KLIPPERVAULT_DB_PATH", "")).strip()
	if override:
		return Path(override).expanduser().resolve()
	return (Path.home() / ".local" / "share" / "klippervault" / "klipper_macros.db").resolve()


DEFAULT_CONFIG_DIR = str(_default_config_dir())
DEFAULT_DB_PATH = str(_default_db_path())
