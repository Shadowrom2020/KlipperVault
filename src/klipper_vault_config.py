#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read and write the KlipperVault configuration file (klippervault.cfg)."""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path

_CFG_FILENAME = "klippervault.cfg"

_DEFAULT_CONTENT = """\
# KlipperVault configuration
# This file is automatically created by KlipperVault on first start.
# Edit the values below to customise behaviour.

[vault]
# Maximum number of versions to keep per macro.
# Older versions are deleted automatically when this limit is exceeded.
# Minimum value is 1.
version_history_size: 5

# HTTP port for the KlipperVault web UI.
port: 10090

# UI language used by the web interface (for example: en, de).
ui_language: en

# Optional printer identity fields used by KlipperVault features.
# If left empty, KlipperVault asks once on first start.
printer_vendor:
printer_model:
"""


@dataclass
class VaultConfig:
    version_history_size: int = 5
    port: int = 10090
    ui_language: str = "en"
    printer_vendor: str = ""
    printer_model: str = ""
    printer_profile_prompt_required: bool = True


def save(config_dir: Path, config: VaultConfig) -> None:
    """Persist VaultConfig to klippervault.cfg in a stable Klipper format."""
    config_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = config_dir / _CFG_FILENAME

    lines = [
        "# KlipperVault configuration",
        "# This file is automatically created by KlipperVault on first start.",
        "# Edit the values below to customise behaviour.",
        "",
        "[vault]",
        "# Maximum number of versions to keep per macro.",
        "# Older versions are deleted automatically when this limit is exceeded.",
        "# Minimum value is 1.",
        f"version_history_size: {max(1, int(config.version_history_size))}",
        "",
        "# HTTP port for the KlipperVault web UI.",
        f"port: {int(config.port)}",
        "",
        "# UI language used by the web interface (for example: en, de).",
        f"ui_language: {str(config.ui_language or 'en').strip().lower() or 'en'}",
        "",
        "# Optional printer identity fields used by KlipperVault features.",
        "# If left empty, KlipperVault asks once on first start.",
        f"printer_vendor: {str(config.printer_vendor or '').strip()}",
        f"printer_model: {str(config.printer_model or '').strip()}",
        "",
    ]
    cfg_path.write_text("\n".join(lines), encoding="utf-8")


def load_or_create(config_dir: Path) -> VaultConfig:
    """Load klippervault.cfg from *config_dir*, creating it with defaults if absent.

    The file is written in Klipper cfg format so it can live alongside
    printer.cfg and other Klipper configuration files.
    """
    cfg_path = config_dir / _CFG_FILENAME

    if not cfg_path.exists():
        config_dir.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(_DEFAULT_CONTENT, encoding="utf-8")

    parser = configparser.ConfigParser()
    parser.read(str(cfg_path), encoding="utf-8")

    version_history_size = 5
    port = 10090
    if parser.has_option("vault", "version_history_size"):
        try:
            version_history_size = max(1, int(parser.get("vault", "version_history_size")))
        except ValueError:
            pass

    if parser.has_option("vault", "port"):
        try:
            parsed_port = int(parser.get("vault", "port"))
            if 1 <= parsed_port <= 65535:
                port = parsed_port
        except ValueError:
            pass

    ui_language = "en"
    if parser.has_option("vault", "ui_language"):
        raw_language = parser.get("vault", "ui_language").strip().lower()
        if raw_language:
            ui_language = raw_language

    printer_vendor = ""
    vendor_is_stored = False
    if parser.has_option("vault", "printer_vendor"):
        vendor_is_stored = True
        printer_vendor = parser.get("vault", "printer_vendor").strip()

    printer_model = ""
    model_is_stored = False
    if parser.has_option("vault", "printer_model"):
        model_is_stored = True
        printer_model = parser.get("vault", "printer_model").strip()

    # Prompt on first start and on upgrades where old cfg files do not yet
    # contain these keys, or when stored values are still empty.
    printer_profile_prompt_required = (
        not vendor_is_stored
        or not model_is_stored
        or not printer_vendor
        or not printer_model
    )

    return VaultConfig(
        version_history_size=version_history_size,
        port=port,
        ui_language=ui_language,
        printer_vendor=printer_vendor,
        printer_model=printer_model,
        printer_profile_prompt_required=printer_profile_prompt_required,
    )
