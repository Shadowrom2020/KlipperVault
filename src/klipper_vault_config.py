#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read and write the KlipperVault configuration file (klippervault.cfg)."""

from __future__ import annotations

import configparser
from dataclasses import dataclass, fields as dataclass_fields
from pathlib import Path

_CFG_FILENAME = "klippervault.cfg"
_FREEDI_CFG_FILENAME = "freedi.cfg"
_DEFAULT_ONLINE_UPDATE_REPO_URL = "https://github.com/Shadowrom2020/KlipperVault-Online-Updates"
_DEFAULT_ONLINE_UPDATE_MANIFEST_PATH = "updates/manifest.json"
_DEFAULT_ONLINE_UPDATE_REF = "main"

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

# Optional GitHub source for online macro updates.
# Example: https://github.com/<owner>/<repo>
online_update_repo_url: https://github.com/Shadowrom2020/KlipperVault-Online-Updates

# Manifest file path inside the repository.
online_update_manifest_path: updates/manifest.json

# Branch, tag, or commit used for update checks.
online_update_ref: main

# Developer mode: enables export of local macros to update repository bundles.
# WARNING: This is intended for repository maintainers; keep disabled for normal use.
developer: false
"""


@dataclass
class VaultConfig:
    version_history_size: int = 5
    port: int = 10090
    ui_language: str = "en"
    printer_vendor: str = ""
    printer_model: str = ""
    online_update_repo_url: str = _DEFAULT_ONLINE_UPDATE_REPO_URL
    online_update_manifest_path: str = _DEFAULT_ONLINE_UPDATE_MANIFEST_PATH
    online_update_ref: str = _DEFAULT_ONLINE_UPDATE_REF
    developer: bool = False
    printer_profile_prompt_required: bool = True


def _persisted_config_keys() -> set[str]:
    """Return config keys that should be stored in klippervault.cfg."""
    return {
        field.name
        for field in dataclass_fields(VaultConfig)
        if field.name != "printer_profile_prompt_required"
    }


def _missing_persisted_config_keys(parser: configparser.ConfigParser) -> set[str]:
    """Return persisted config keys missing from the [vault] section."""
    return {
        key for key in _persisted_config_keys() if not parser.has_option("vault", key)
    }


def _read_key_value_line(raw_line: str) -> tuple[str, str] | None:
    """Parse simple cfg lines like `key: value` or `key = value`."""
    line = raw_line.split("#", 1)[0].strip()
    if not line:
        return None
    for separator in (":", "="):
        if separator in line:
            key, value = line.split(separator, 1)
            key = key.strip().lower()
            value = value.strip()
            if key:
                return key, value
    return None


def _detect_printer_identity(config_dir: Path) -> tuple[str, str] | None:
    """Detect printer identity from known vendor-specific config files."""
    freedi_cfg_path = config_dir / _FREEDI_CFG_FILENAME
    if not freedi_cfg_path.exists():
        return None

    printer_model = ""
    for raw_line in freedi_cfg_path.read_text(encoding="utf-8").splitlines():
        parsed_line = _read_key_value_line(raw_line)
        if parsed_line is None:
            continue
        key, value = parsed_line
        if key == "printer_model" and value:
            printer_model = value
            break

    if printer_model:
        return "freedi", printer_model

    return None


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
        "# Optional GitHub source for online macro updates.",
        "# Example: https://github.com/<owner>/<repo>",
        f"online_update_repo_url: {str(config.online_update_repo_url or _DEFAULT_ONLINE_UPDATE_REPO_URL).strip() or _DEFAULT_ONLINE_UPDATE_REPO_URL}",
        "",
        "# Manifest file path inside the repository.",
        f"online_update_manifest_path: {str(config.online_update_manifest_path or _DEFAULT_ONLINE_UPDATE_MANIFEST_PATH).strip() or _DEFAULT_ONLINE_UPDATE_MANIFEST_PATH}",
        "",
        "# Branch, tag, or commit used for update checks.",
        f"online_update_ref: {str(config.online_update_ref or _DEFAULT_ONLINE_UPDATE_REF).strip() or _DEFAULT_ONLINE_UPDATE_REF}",
        "",
        "# Developer mode: enables export of local macros to update repository bundles.",
        "# WARNING: This is intended for repository maintainers; keep disabled for normal use.",
        f"developer: {'true' if config.developer else 'false'}",
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
    missing_persisted_keys = _missing_persisted_config_keys(parser)

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

    online_update_repo_url = _DEFAULT_ONLINE_UPDATE_REPO_URL
    if parser.has_option("vault", "online_update_repo_url"):
        online_update_repo_url = parser.get("vault", "online_update_repo_url").strip()

    online_update_manifest_path = _DEFAULT_ONLINE_UPDATE_MANIFEST_PATH
    if parser.has_option("vault", "online_update_manifest_path"):
        parsed_manifest_path = parser.get("vault", "online_update_manifest_path").strip()
        if parsed_manifest_path:
            online_update_manifest_path = parsed_manifest_path

    online_update_ref = _DEFAULT_ONLINE_UPDATE_REF
    if parser.has_option("vault", "online_update_ref"):
        parsed_ref = parser.get("vault", "online_update_ref").strip()
        if parsed_ref:
            online_update_ref = parsed_ref

    developer = False
    if parser.has_option("vault", "developer"):
        dev_value = parser.get("vault", "developer").strip().lower()
        developer = dev_value in ("true", "1", "yes")

    detected_printer_identity = False
    if not printer_vendor or not printer_model:
        detected_identity = _detect_printer_identity(config_dir)
        if detected_identity is not None:
            printer_vendor, printer_model = detected_identity
            vendor_is_stored = True
            model_is_stored = True
            detected_printer_identity = True

    # Prompt on first start and on upgrades where old cfg files do not yet
    # contain these keys, or when stored values are still empty.
    printer_profile_prompt_required = (
        not vendor_is_stored
        or not model_is_stored
        or not printer_vendor
        or not printer_model
    )

    config = VaultConfig(
        version_history_size=version_history_size,
        port=port,
        ui_language=ui_language,
        printer_vendor=printer_vendor,
        printer_model=printer_model,
        online_update_repo_url=online_update_repo_url,
        online_update_manifest_path=online_update_manifest_path,
        online_update_ref=online_update_ref,
        developer=developer,
        printer_profile_prompt_required=printer_profile_prompt_required,
    )

    should_backfill_config = (
        detected_printer_identity
        or bool(missing_persisted_keys)
    )

    if should_backfill_config:
        save(config_dir, config)

    return config
