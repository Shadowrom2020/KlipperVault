#!/usr/bin/env python3
# Copyright (C) 2026 Juergen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared default filesystem paths for KlipperVault runtimes."""

from __future__ import annotations

from pathlib import Path


DEFAULT_CONFIG_DIR = str((Path.home() / "printer_data" / "config").resolve())
DEFAULT_DB_PATH = str((Path.home() / "printer_data" / "db" / "klipper_macros.db").resolve())
