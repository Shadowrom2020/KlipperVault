#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read and write KlipperVault configuration values in SQLite."""

from __future__ import annotations

import configparser
from dataclasses import dataclass, fields as dataclass_fields
from pathlib import Path
import time

from klipper_vault_db import open_sqlite_connection

_CFG_FILENAME = "klippervault.cfg"
_SETTINGS_TABLE = "vault_settings"
_DEFAULT_ONLINE_UPDATE_REPO_URL = "https://github.com/Shadowrom2020/KlipperVault-Online-Updates"
_DEFAULT_ONLINE_UPDATE_MANIFEST_PATH = "updates/manifest.json"
_DEFAULT_ONLINE_UPDATE_REF = "main"

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
    """Return config keys that should be stored in persistent settings."""
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


def _default_db_path(config_dir: Path) -> Path:
    """Return fallback DB path used in unit tests and standalone calls."""
    return config_dir / "klippervault_settings.db"


def _normalized_config(config: VaultConfig) -> VaultConfig:
    """Return one normalized configuration payload with clamped defaults."""
    version_history_size = max(int(config.version_history_size), 1)
    port = int(config.port)
    if port < 1 or port > 65535:
        port = 10090
    ui_language = str(config.ui_language or "en").strip().lower() or "en"
    runtime_mode = "off_printer"
    printer_vendor = str(config.printer_vendor or "").strip()
    printer_model = str(config.printer_model or "").strip()
    online_update_repo_url = (
        str(config.online_update_repo_url or _DEFAULT_ONLINE_UPDATE_REPO_URL).strip()
        or _DEFAULT_ONLINE_UPDATE_REPO_URL
    )
    online_update_manifest_path = (
        str(config.online_update_manifest_path or _DEFAULT_ONLINE_UPDATE_MANIFEST_PATH).strip()
        or _DEFAULT_ONLINE_UPDATE_MANIFEST_PATH
    )
    online_update_ref = (
        str(config.online_update_ref or _DEFAULT_ONLINE_UPDATE_REF).strip()
        or _DEFAULT_ONLINE_UPDATE_REF
    )
    developer = bool(config.developer)

    return VaultConfig(
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
        printer_profile_prompt_required=(not printer_vendor or not printer_model),
    )


def ensure_settings_schema(conn) -> None:
    """Ensure SQLite table for global app settings exists."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_SETTINGS_TABLE} (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )


def _settings_rows(conn) -> dict[str, str]:
    """Load all persisted settings as key/value mapping."""
    rows = conn.execute(f"SELECT key, value FROM {_SETTINGS_TABLE}").fetchall()
    return {str(key): str(value) for key, value in rows}


def _persist_config(conn, config: VaultConfig) -> None:
    """Write normalized settings to SQLite."""
    normalized = _normalized_config(config)
    now_ts = int(time.time())
    payload = {
        "version_history_size": str(normalized.version_history_size),
        "port": str(normalized.port),
        "runtime_mode": normalized.runtime_mode,
        "ui_language": normalized.ui_language,
        "printer_vendor": normalized.printer_vendor,
        "printer_model": normalized.printer_model,
        "online_update_repo_url": normalized.online_update_repo_url,
        "online_update_manifest_path": normalized.online_update_manifest_path,
        "online_update_ref": normalized.online_update_ref,
        "developer": "true" if normalized.developer else "false",
    }
    for key, value in payload.items():
        conn.execute(
            f"""
            INSERT INTO {_SETTINGS_TABLE} (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (key, value, now_ts),
        )


def _config_from_rows(rows: dict[str, str]) -> VaultConfig:
    """Build normalized VaultConfig from SQLite settings rows."""
    parser = configparser.ConfigParser()
    parser.add_section("vault")
    for key, value in rows.items():
        parser.set("vault", key, value)

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
    printer_vendor = _get_stripped(parser, "vault", "printer_vendor", default="")
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

    return _normalized_config(
        VaultConfig(
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
        )
    )


def _load_legacy_cfg(config_dir: Path) -> VaultConfig | None:
    """Load legacy cfg settings when a file exists; return None otherwise."""
    cfg_path = config_dir / _CFG_FILENAME
    if not cfg_path.exists():
        return None

    parser = configparser.ConfigParser()
    parser.read(str(cfg_path), encoding="utf-8")
    return _config_from_rows({
        key: parser.get("vault", key)
        for key in _persisted_config_keys()
        if parser.has_option("vault", key)
    })


def save(config_dir: Path, config: VaultConfig, db_path: Path | None = None) -> None:
    """Persist VaultConfig into SQLite-backed app settings."""
    target_db_path = Path(db_path) if db_path is not None else _default_db_path(config_dir)
    with open_sqlite_connection(target_db_path, ensure_schema=ensure_settings_schema) as conn:
        _persist_config(conn, config)
        conn.commit()


def load_or_create(config_dir: Path, db_path: Path | None = None) -> VaultConfig:
    """Load app settings from SQLite and migrate legacy cfg values when needed."""
    target_db_path = Path(db_path) if db_path is not None else _default_db_path(config_dir)
    with open_sqlite_connection(target_db_path, ensure_schema=ensure_settings_schema) as conn:
        rows = _settings_rows(conn)
        if rows:
            config = _config_from_rows(rows)
            _persist_config(conn, config)
            conn.commit()
            return config

        migrated = _load_legacy_cfg(config_dir)
        config = migrated if migrated is not None else VaultConfig()
        config = _normalized_config(config)
        _persist_config(conn, config)
        conn.commit()
        return config
