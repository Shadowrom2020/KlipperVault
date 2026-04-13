#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Dedicated GUI launcher for KlipperVault."""

from __future__ import annotations

from pathlib import Path

from klipper_vault import (
    REPO_ROOT,
    _load_app_version,
    _patch_nicegui_deleted_parent_slot_event_race,
    _patch_nicegui_deleted_parent_slot_exception_filter,
    _patch_nicegui_disconnect_signature,
    _sync_venv_requirements_if_needed,
)


def main() -> None:
    """Start the KlipperVault GUI runtime."""
    _sync_venv_requirements_if_needed()

    from klipper_macro_gui import build_ui
    from klipper_vault_config import load_or_create as _load_vault_config
    from klipper_vault_i18n import t
    from klipper_vault_paths import DEFAULT_CONFIG_DIR, DEFAULT_DB_PATH
    from nicegui import ui

    _patch_nicegui_disconnect_signature()
    _patch_nicegui_deleted_parent_slot_event_race()
    _patch_nicegui_deleted_parent_slot_exception_filter()

    config_dir = Path(DEFAULT_CONFIG_DIR).expanduser().resolve()
    db_path = Path(DEFAULT_DB_PATH).expanduser().resolve()
    vault_cfg = _load_vault_config(config_dir, db_path)
    favicon_path = REPO_ROOT / "assets" / "favicon.svg"
    build_ui(app_version=_load_app_version())
    # Intentional: the web UI must be reachable from other devices on the LAN.
    ui.run(
        host="0.0.0.0",  # nosec B104
        port=vault_cfg.port,
        title=t("Klipper Vault"),
        dark=True,
        favicon=favicon_path,
        show=False,
        reload=False,
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()