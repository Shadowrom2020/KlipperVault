#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read and write the KlipperVault configuration file (klippervault.cfg)."""

from __future__ import annotations

import configparser
from dataclasses import dataclass, fields as dataclass_fields
from pathlib import Path

_CFG_FILENAME = "klippervault.cfg"
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

# Runtime mode controls where connection settings come from.
# KlipperVault now runs in remote mode only.
runtime_mode: off_printer

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
    runtime_mode: str = "off_printer"
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


def _get_stripped(
    parser: configparser.ConfigParser,
    section: str,
    option: str,
    *,
    default: str,
    lower: bool = False,
    require_non_empty: bool = False,
) -> str:
    """Read one config option as stripped text with optional normalization."""
    if not parser.has_option(section, option):
        return default
    value = parser.get(section, option).strip()
    if lower:
        value = value.lower()
    if require_non_empty and not value:
        return default
    return value


def _get_int_in_range(
    parser: configparser.ConfigParser,
    section: str,
    option: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
    clamp_below_minimum: bool = False,
    clamp_above_maximum: bool = False,
) -> int:
    """Read one config option as int constrained to an inclusive range."""
    if not parser.has_option(section, option):
        return default
    try:
        value = int(parser.get(section, option))
    except ValueError:
        return default
    if value < minimum and clamp_below_minimum:
        return minimum
    if value > maximum and clamp_above_maximum:
        return maximum
    if minimum <= value <= maximum:
        return value
    return default


def _get_bool(
    parser: configparser.ConfigParser,
    section: str,
    option: str,
    *,
    default: bool,
) -> bool:
    """Read one config option as a legacy-compatible boolean toggle."""
    if not parser.has_option(section, option):
        return default
    value = parser.get(section, option).strip().lower()
    return value in ("true", "1", "yes")


def _get_enum(
    parser: configparser.ConfigParser,
    section: str,
    option: str,
    *,
    default: str,
    allowed: set[str],
) -> str:
    """Read one config option constrained to a predefined set of values."""
    value = _get_stripped(
        parser,
        section,
        option,
        default=default,
        lower=True,
        require_non_empty=True,
    )
    return value if value in allowed else default


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
        "# Runtime mode controls where connection settings come from.",
        "# KlipperVault now runs in remote mode only.",
        "runtime_mode: off_printer",
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

    version_history_size = _get_int_in_range(
        parser,
        "vault",
        "version_history_size",
        default=5,
        minimum=1,
        maximum=2_147_483_647,
        clamp_below_minimum=True,
    )
    port = _get_int_in_range(
        parser,
        "vault",
        "port",
        default=10090,
        minimum=1,
        maximum=65535,
    )
    ui_language = _get_stripped(
        parser,
        "vault",
        "ui_language",
        default="en",
        lower=True,
        require_non_empty=True,
    )
    runtime_mode = _get_enum(
        parser,
        "vault",
        "runtime_mode",
        default="off_printer",
        allowed={"off_printer"},
    )

    printer_vendor = ""
    vendor_is_stored = False
    if parser.has_option("vault", "printer_vendor"):
        vendor_is_stored = True
        printer_vendor = _get_stripped(parser, "vault", "printer_vendor", default="")

    printer_model = ""
    model_is_stored = False
    if parser.has_option("vault", "printer_model"):
        model_is_stored = True
        printer_model = _get_stripped(parser, "vault", "printer_model", default="")

    online_update_repo_url = _get_stripped(
        parser,
        "vault",
        "online_update_repo_url",
        default=_DEFAULT_ONLINE_UPDATE_REPO_URL,
    )
    online_update_manifest_path = _get_stripped(
        parser,
        "vault",
        "online_update_manifest_path",
        default=_DEFAULT_ONLINE_UPDATE_MANIFEST_PATH,
        require_non_empty=True,
    )
    online_update_ref = _get_stripped(
        parser,
        "vault",
        "online_update_ref",
        default=_DEFAULT_ONLINE_UPDATE_REF,
        require_non_empty=True,
    )

    developer = _get_bool(parser, "vault", "developer", default=False)

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
        runtime_mode=runtime_mode,
        ui_language=ui_language,
        printer_vendor=printer_vendor,
        printer_model=printer_model,
        online_update_repo_url=online_update_repo_url,
        online_update_manifest_path=online_update_manifest_path,
        online_update_ref=online_update_ref,
        developer=developer,
        printer_profile_prompt_required=printer_profile_prompt_required,
    )

    should_backfill_config = bool(missing_persisted_keys)

    if should_backfill_config:
        save(config_dir, config)

    return config
