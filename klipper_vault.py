#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Root launcher for the KlipperVault NiceGUI app."""

from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    # Keep imports stable after moving all source modules under src/.
    sys.path.insert(0, str(SRC_DIR))

from klipper_macro_gui import DEFAULT_CONFIG_DIR, build_ui  # noqa: E402
from klipper_vault_config import load_or_create as _load_vault_config  # noqa: E402
from klipper_vault_i18n import t  # noqa: E402
from nicegui import ui  # noqa: E402


def _load_app_version() -> str:
    """Read application version from VERSION file, with safe fallback."""
    version_path = REPO_ROOT / "VERSION"
    try:
        version = version_path.read_text(encoding="utf-8").strip()
        return version or "unknown"
    except OSError:
        return "unknown"


def main() -> None:
    """Start the NiceGUI application with configured runtime settings."""
    config_dir = Path(DEFAULT_CONFIG_DIR).expanduser().resolve()
    vault_cfg = _load_vault_config(config_dir)
    favicon_path = REPO_ROOT / "assets" / "favicon.svg"
    build_ui(app_version=_load_app_version())
    ui.run(
        host="0.0.0.0", # nosec B104 - intentional: GUI must be reachable on LAN
        port=vault_cfg.port,
        title=t("Klipper Vault"),
        dark=True,
        favicon=favicon_path,
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()
