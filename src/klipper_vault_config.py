#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read and write KlipperVault configuration values in SQLite."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time

from klipper_vault_db import open_sqlite_connection

_SETTINGS_TABLE = "vault_settings"
_FIXED_WEB_UI_PORT = 10090
_DEFAULT_ONLINE_UPDATE_REPO_URL = "https://github.com/Shadowrom2020/KlipperVault-Online-Updates"
_DEFAULT_ONLINE_UPDATE_REF = "main"
_ALLOWED_THEME_MODES = {"auto", "light", "dark"}

@dataclass
class VaultConfig:
    version_history_size: int = 5
    port: int = _FIXED_WEB_UI_PORT
    runtime_mode: str = "standard"
    ui_language: str = "en"
    printer_vendor: str = ""
    printer_model: str = ""
    online_update_repo_url: str = _DEFAULT_ONLINE_UPDATE_REPO_URL
    online_update_ref: str = _DEFAULT_ONLINE_UPDATE_REF
    theme_mode: str = "auto"
    developer: bool = False
    printer_profile_prompt_required: bool = True
    macro_migration_prompt_enabled: bool = True


def _read_str(rows: dict[str, str], key: str, *, default: str, lower: bool = False, require_non_empty: bool = False) -> str:
    """Read one string setting with optional normalization rules."""
    value = str(rows.get(key, default)).strip()
    if lower:
        value = value.lower()
    if require_non_empty and not value:
        return default
    return value


def _read_int(rows: dict[str, str], key: str, *, default: int, minimum: int, maximum: int, clamp_below_minimum: bool = False) -> int:
    """Read one integer setting constrained to an inclusive range."""
    raw = str(rows.get(key, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < minimum and clamp_below_minimum:
        return minimum
    if minimum <= value <= maximum:
        return value
    return default


def _read_bool(rows: dict[str, str], key: str, *, default: bool) -> bool:
    """Read one boolean setting from sqlite text payload."""
    raw = str(rows.get(key, "")).strip().lower()
    if not raw:
        return default
    return raw in {"true", "1", "yes"}


def _default_db_path(config_dir: Path) -> Path:
    """Return fallback DB path used in unit tests and standalone calls."""
    return config_dir / "klippervault_settings.db"


def _normalized_config(config: VaultConfig) -> VaultConfig:
    """Return one normalized configuration payload with clamped defaults."""
    version_history_size = max(int(config.version_history_size), 1)
    port = _FIXED_WEB_UI_PORT
    ui_language = str(config.ui_language or "en").strip().lower() or "en"
    runtime_mode = "standard"
    printer_vendor = str(config.printer_vendor or "").strip()
    printer_model = str(config.printer_model or "").strip()
    online_update_repo_url = (
        str(config.online_update_repo_url or _DEFAULT_ONLINE_UPDATE_REPO_URL).strip()
        or _DEFAULT_ONLINE_UPDATE_REPO_URL
    )
    online_update_ref = (
        str(config.online_update_ref or _DEFAULT_ONLINE_UPDATE_REF).strip()
        or _DEFAULT_ONLINE_UPDATE_REF
    )
    theme_mode = str(config.theme_mode or "auto").strip().lower() or "auto"
    if theme_mode not in _ALLOWED_THEME_MODES:
        theme_mode = "auto"
    developer = bool(config.developer)
    macro_migration_prompt_enabled = bool(config.macro_migration_prompt_enabled)

    return VaultConfig(
        version_history_size=version_history_size,
        port=port,
        runtime_mode=runtime_mode,
        ui_language=ui_language,
        printer_vendor=printer_vendor,
        printer_model=printer_model,
        online_update_repo_url=online_update_repo_url,
        online_update_ref=online_update_ref,
        theme_mode=theme_mode,
        developer=developer,
        printer_profile_prompt_required=(not printer_vendor or not printer_model),
        macro_migration_prompt_enabled=macro_migration_prompt_enabled,
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
        "online_update_ref": normalized.online_update_ref,
        "theme_mode": normalized.theme_mode,
        "developer": "true" if normalized.developer else "false",
        "macro_migration_prompt_enabled": "true" if normalized.macro_migration_prompt_enabled else "false",
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
    version_history_size = _read_int(
        rows,
        "version_history_size",
        default=5,
        minimum=1,
        maximum=2_147_483_647,
        clamp_below_minimum=True,
    )
    port = _read_int(
        rows,
        "port",
        default=_FIXED_WEB_UI_PORT,
        minimum=1,
        maximum=65535,
    )
    port = _FIXED_WEB_UI_PORT
    ui_language = _read_str(
        rows,
        "ui_language",
        default="en",
        lower=True,
        require_non_empty=True,
    )
    runtime_mode = "standard"
    printer_vendor = _read_str(rows, "printer_vendor", default="")
    printer_model = _read_str(rows, "printer_model", default="")
    online_update_repo_url = _read_str(
        rows,
        "online_update_repo_url",
        default=_DEFAULT_ONLINE_UPDATE_REPO_URL,
    )
    online_update_ref = _read_str(
        rows,
        "online_update_ref",
        default=_DEFAULT_ONLINE_UPDATE_REF,
        require_non_empty=True,
    )
    theme_mode = _read_str(
        rows,
        "theme_mode",
        default="auto",
        lower=True,
        require_non_empty=True,
    )
    developer = _read_bool(rows, "developer", default=False)
    macro_migration_prompt_enabled = _read_bool(rows, "macro_migration_prompt_enabled", default=True)

    return _normalized_config(
        VaultConfig(
            version_history_size=version_history_size,
            port=port,
            runtime_mode=runtime_mode,
            ui_language=ui_language,
            printer_vendor=printer_vendor,
            printer_model=printer_model,
            online_update_repo_url=online_update_repo_url,
            online_update_ref=online_update_ref,
            theme_mode=theme_mode,
            developer=developer,
            macro_migration_prompt_enabled=macro_migration_prompt_enabled,
        )
    )


def save(config_dir: Path, config: VaultConfig, db_path: Path | None = None) -> None:
    """Persist VaultConfig into SQLite-backed app settings."""
    target_db_path = Path(db_path) if db_path is not None else _default_db_path(config_dir)
    with open_sqlite_connection(target_db_path, ensure_schema=ensure_settings_schema) as conn:
        _persist_config(conn, config)
        conn.commit()


def load_or_create(config_dir: Path, db_path: Path | None = None) -> VaultConfig:
    """Load app settings from SQLite and bootstrap defaults when empty."""
    target_db_path = Path(db_path) if db_path is not None else _default_db_path(config_dir)
    with open_sqlite_connection(target_db_path, ensure_schema=ensure_settings_schema) as conn:
        rows = _settings_rows(conn)
        if rows:
            config = _config_from_rows(rows)
            _persist_config(conn, config)
            conn.commit()
            return config

        config = VaultConfig()
        config = _normalized_config(config)
        _persist_config(conn, config)
        conn.commit()
        return config
