#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Backup/restore mixin for MacroGuiService.

Extracted from klipper_macro_gui_service to keep concern-specific logic
in focused, navigable modules. MacroGuiService inherits this mixin.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from klipper_vault_config_source import LocalConfigSource

from klipper_macro_backup import (
    backup_printer_cfg_restore_policy,
    create_macro_backup,
    delete_macro_backup,
    list_macro_backups,
    load_backup_items,
    restore_macro_backup,
)


class BackupRestoreMixin:
    """Backup and restore operations mixed into MacroGuiService."""

    # ------------------------------------------------------------------
    # Attributes provided by MacroGuiService at runtime
    # ------------------------------------------------------------------
    _db_path: Path
    _active_printer_profile_id: int
    _runtime_mode: str

    def _resolve_runtime_config_dir(self) -> Path:
        raise NotImplementedError  # provided by MacroGuiService

    def _runtime_local_config_source(self) -> LocalConfigSource:
        raise NotImplementedError  # provided by PrinterProfileMixin

    def sync_active_remote_cfg_to_local(
        self,
        *,
        prune_missing: bool = True,
        progress_callback=None,
    ) -> dict[str, object]:
        raise NotImplementedError  # provided by PrinterProfileMixin

    # ------------------------------------------------------------------

    def create_backup(self, name: str) -> dict[str, object]:
        """Create a named backup snapshot from current macro state."""
        runtime_config_dir = self._resolve_runtime_config_dir()
        runtime_source = self._runtime_local_config_source()

        # In off-printer mode, backups must reflect the current printer cfg
        # tree, not potentially stale runtime cache files.
        if str(self._runtime_mode).strip().lower() == "off_printer":
            try:
                self.sync_active_remote_cfg_to_local(prune_missing=True)
            except Exception:
                # Allow local/test flows without active SSH profile to proceed
                # when runtime cache already has cfg files.
                if not runtime_source.list_cfg_files():
                    raise

        return create_macro_backup(
            db_path=self._db_path,
            backup_name=name,
            config_dir=runtime_config_dir,
            config_source=runtime_source,
            printer_profile_id=self._active_printer_profile_id,
        )

    def list_backups(self) -> list[dict[str, object]]:
        """Return all available backups."""
        return list_macro_backups(self._db_path, printer_profile_id=self._active_printer_profile_id)

    def load_backup_contents(self, backup_id: int) -> list[dict[str, object]]:
        """Return snapshot items for one backup."""
        return load_backup_items(self._db_path, backup_id, printer_profile_id=self._active_printer_profile_id)

    def get_backup_restore_policy(self, backup_id: int) -> dict[str, object]:
        """Return restore policy metadata for one backup."""
        return backup_printer_cfg_restore_policy(
            db_path=self._db_path,
            backup_id=backup_id,
            printer_profile_id=self._active_printer_profile_id,
        )

    def restore_backup(
        self,
        backup_id: int,
    ) -> dict[str, object]:
        """Restore selected backup state to db/local cfg files."""
        result = restore_macro_backup(
            db_path=self._db_path,
            backup_id=backup_id,
            config_dir=self._resolve_runtime_config_dir(),
            config_source=self._runtime_local_config_source(),
            printer_profile_id=self._active_printer_profile_id,
        )
        result["remote_synced"] = False
        result["local_changed"] = True
        return result

    def delete_backup(self, backup_id: int) -> dict[str, object]:
        """Delete one backup snapshot."""
        return delete_macro_backup(
            db_path=self._db_path,
            backup_id=backup_id,
            printer_profile_id=self._active_printer_profile_id,
        )
