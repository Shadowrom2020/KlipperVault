#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Service layer for KlipperVault GUI actions.

This module keeps database/config operations outside UI code so the NiceGUI
module can focus on rendering and user interactions.

MacroGuiService composes three focused mixin classes:
  - PrinterProfileMixin  (klipper_macro_service_profiles)
  - BackupRestoreMixin   (klipper_macro_service_backup)
  - OnlineUpdateMixin    (klipper_macro_service_online)
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import shutil
import tempfile
import atexit
from pathlib import Path
from typing import Callable

from klipper_macro_indexer import (
    delete_macro_from_cfg,
    get_cfg_loading_overview,
    get_cfg_loading_overview_from_source,
    load_duplicate_macro_groups,
    load_macro_list,
    load_macro_versions,
    load_stats,
    migrate_printer_cfg_macros_to_macros_cfg,
    macro_row_to_section_text,
    parse_macros_from_cfg,
    remove_all_deleted_macros,
    remove_deleted_macro,
    remove_inactive_macro_version,
    restore_macro_version,
    resolve_duplicate_macros,
    run_indexing_from_source,
    save_macro_edit,
)
from klipper_vault_db import open_sqlite_connection
from klipper_vault_remote_profiles import ensure_remote_profile_schema
from klipper_vault_secret_store import CredentialStore
from klipper_vault_printer_profiles import (
    ensure_default_printer_profile,
    ensure_printer_profile_schema,
)
from klipper_type_utils import to_int as _as_int
from klipper_type_utils import to_text as _as_text

from klipper_macro_service_profiles import PrinterProfileMixin
from klipper_macro_service_backup import BackupRestoreMixin
from klipper_macro_service_online import OnlineUpdateMixin


_PROTECTED_CFG_FILENAME = "printer.cfg"


def _cfg_is_protected(file_path: str) -> bool:
    """Return True when cfg path points to protected printer.cfg."""
    return Path(_as_text(file_path)).name.lower() == _PROTECTED_CFG_FILENAME


class MacroGuiService(PrinterProfileMixin, BackupRestoreMixin, OnlineUpdateMixin):
    """Coordinates backend operations used by the GUI layer."""

    def __init__(
        self,
        db_path: Path,
        config_dir: Path,
        version_history_size: int,
        runtime_mode: str = "standard",
        moonraker_base_url: str = "",
    ) -> None:
        self._db_path = db_path
        self._config_dir = config_dir
        self._version_history_size = version_history_size
        self._runtime_mode = "standard"
        self._moonraker_base_url = moonraker_base_url.rstrip("/")
        self._cache_base_dir = Path(tempfile.gettempdir()) / "klippervault"
        self._active_cache_dir: Path | None = None
        self._active_cache_printer_profile_id: int | None = None
        self._remote_cfg_checksums: dict[str, str] = {}
        self._credential_store = CredentialStore(self._db_path)
        atexit.register(self.cleanup_runtime_cache)
        # Prepare standard profile/credential tables before SSH features are wired into UI.
        with open_sqlite_connection(self._db_path, ensure_schema=ensure_remote_profile_schema) as conn:
            conn.commit()
        with open_sqlite_connection(self._db_path, ensure_schema=ensure_printer_profile_schema) as conn:
            conn.commit()
        self._active_printer_profile_id = int(ensure_default_printer_profile(self._db_path))

        # In standard mode the active SSH profile is the source of Moonraker routing.
        self._refresh_moonraker_base_url_from_active_profile()

    @staticmethod
    def _protected_file_block_message(file_path: str) -> str:
        """Build user-facing rationale for protected printer.cfg operations."""
        return (
            f"Macros in {_PROTECTED_CFG_FILENAME} are read-only in KlipperVault. "
            "This file may contain critical printer settings, so automated updates are blocked. "
            "Move the macro to a separate included .cfg file to enable updates."
        )

    @staticmethod
    def _emit_operation_progress(
        callback: Callable[[str, int, int], None] | None,
        phase: str,
        current: int,
        total: int,
    ) -> None:
        """Emit normalized operation progress payload to UI callback."""
        if callback is None:
            return
        callback(str(phase), max(int(current), 0), max(int(total), 1))

    def get_runtime_config_dir(self) -> Path:
        """Return active config root used for parser and file mutations."""
        return self._resolve_runtime_config_dir()

    def set_version_history_size(self, value: int) -> None:
        """Update in-memory version history retention for subsequent indexing runs."""
        self._version_history_size = max(int(value), 1)

    def cleanup_runtime_cache(self) -> None:
        """Remove active standard cache directory when available."""
        if self._active_cache_dir is not None:
            shutil.rmtree(self._active_cache_dir, ignore_errors=True)
        self._active_cache_dir = None
        self._active_cache_printer_profile_id = None
        self._remote_cfg_checksums = {}

    @staticmethod
    def _text_checksum(text: str) -> str:
        """Return stable checksum for cfg content comparisons."""
        return hashlib.sha256(str(text).encode("utf-8", errors="ignore")).hexdigest()

    @staticmethod
    def _remote_conflict_message(rel_path: str, reason: str) -> str:
        """Build a user-actionable message for stale remote cfg conflict detection."""
        return (
            f"Remote cfg conflict for '{rel_path}': {reason}. "
            "Sync remote config again, review differences, and retry the change."
        )

    def _cache_dir_for_profile(self, profile_id: int) -> Path:
        """Create a deterministic per-printer cache directory under system temp."""
        target = self._cache_base_dir / f"printer-{int(profile_id)}"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _resolve_runtime_config_dir(self) -> Path:
        """Resolve effective config root based on current runtime mode/profile."""
        profile_id = int(self._active_printer_profile_id)
        if self._active_cache_printer_profile_id != profile_id:
            self.cleanup_runtime_cache()
            self._active_cache_dir = self._cache_dir_for_profile(profile_id)
            self._active_cache_printer_profile_id = profile_id

        if self._active_cache_dir is None:
            raise RuntimeError("Runtime cache directory is not initialized")
        self._active_cache_dir.mkdir(parents=True, exist_ok=True)
        return self._active_cache_dir

    def _append_restart_policy_result(self, result: dict[str, object], *, uploaded_files: int) -> None:
        """Apply upload metadata without triggering automatic Klipper restarts."""
        if int(uploaded_files) <= 0:
            result["klipper_restarted"] = False
            result["restart_deferred"] = False
            result["restart_message"] = "No restart was triggered because no permitted cfg file was uploaded."
            return

        result["klipper_restarted"] = False
        result["restart_deferred"] = True
        result["restart_message"] = "Config uploaded. Restart Klipper manually when you are ready."

    def index(
        self,
        progress_callback: Callable[[str, int, int], None] | None = None,
        *,
        sync_remote: bool = True,
    ) -> dict[str, object]:
        """Run config indexing with configured retention settings."""
        sync_result: dict[str, object] | None = None
        runtime_config_dir = self._resolve_runtime_config_dir()
        active_profile = self.get_active_printer_profile()
        is_virtual_printer = isinstance(active_profile, dict) and bool(active_profile.get("is_virtual", False))
        effective_sync_remote = bool(sync_remote) and not is_virtual_printer

        if self._runtime_mode == "standard" and effective_sync_remote:
            sync_result = self.sync_active_remote_cfg_to_local(
                prune_missing=True,
                progress_callback=progress_callback,
            )

        def _parse_progress(current: int, total: int) -> None:
            self._emit_operation_progress(progress_callback, "parse", current, total)

        result = run_indexing_from_source(
            config_source=self._runtime_local_config_source(),
            db_path=self._db_path,
            max_versions=self._version_history_size,
            printer_profile_id=self._active_printer_profile_id,
            mark_missing_as_deleted=not is_virtual_printer,
            progress_callback=_parse_progress,
        )
        if sync_result is not None:
            result["remote_sync"] = sync_result
        result["runtime_config_dir"] = str(runtime_config_dir)
        return result

    def load_cfg_loading_overview(self) -> dict[str, object]:
        """Load cfg parse-order overview for Klipper and KlipperVault."""
        if self._runtime_mode == "standard":
            remote_source, _ = self._active_remote_config_source()
            return get_cfg_loading_overview_from_source(remote_source)
        return get_cfg_loading_overview(self._resolve_runtime_config_dir())

    def import_cfg_file_to_runtime(
        self,
        *,
        import_file: Path,
        target_rel_path: str | None = None,
    ) -> dict[str, object]:
        """Import one local .cfg file into the active runtime config directory."""
        source_path = Path(import_file)
        if not source_path.exists() or not source_path.is_file():
            raise ValueError("Import file does not exist")
        if source_path.suffix.lower() != ".cfg":
            raise ValueError("Import file must use .cfg extension")

        target_name = _as_text(target_rel_path or source_path.name).replace("\\", "/").strip()
        if not target_name:
            raise ValueError("Target cfg file path is required")
        if not target_name.lower().endswith(".cfg"):
            raise ValueError("Target cfg file must use .cfg extension")

        source_text = source_path.read_text(encoding="utf-8")
        local_source = self._runtime_local_config_source()
        local_source.write_text(target_name, source_text)

        return {
            "ok": True,
            "imported_path": target_name,
            "runtime_config_dir": str(self._resolve_runtime_config_dir()),
            "bytes": len(source_text.encode("utf-8")),
        }

    def load_dashboard(self, *, limit: int = 500, offset: int = 0) -> tuple[dict[str, object], list[dict[str, object]]]:
        """Load aggregate stats and paged latest macro list for dashboard refresh."""
        return (
            load_stats(self._db_path, printer_profile_id=self._active_printer_profile_id),
            load_macro_list(
                self._db_path,
                limit=limit,
                offset=offset,
                config_source=self._runtime_local_config_source(),
                include_macro_body=False,
                printer_profile_id=self._active_printer_profile_id,
            ),
        )

    def load_versions(self, file_path: str, macro_name: str) -> list[dict[str, object]]:
        """Load version history for a specific macro identity."""
        return load_macro_versions(
            self._db_path,
            file_path,
            macro_name,
            printer_profile_id=self._active_printer_profile_id,
        )

    def load_latest_for_file(self, macro_name: str, file_path: str) -> dict[str, object] | None:
        """Load latest stored row for one macro definition file."""
        versions = self.load_versions(file_path, macro_name)
        return versions[0] if versions else None

    def build_macro_section_text(self, macro: dict[str, object]) -> str:
        """Build editable cfg section text for one macro row."""
        return macro_row_to_section_text(macro)

    def remove_deleted(self, file_path: str, macro_name: str) -> dict[str, object]:
        """Permanently remove a deleted macro history from database."""
        return remove_deleted_macro(
            self._db_path,
            file_path,
            macro_name,
            printer_profile_id=self._active_printer_profile_id,
        )

    def remove_inactive_version(self, file_path: str, macro_name: str, version: int) -> dict[str, object]:
        """Permanently remove one inactive macro version from database."""
        return remove_inactive_macro_version(
            self._db_path,
            file_path,
            macro_name,
            version,
            printer_profile_id=self._active_printer_profile_id,
        )

    def purge_all_deleted(self) -> dict[str, object]:
        """Remove all deleted macro histories from database."""
        return remove_all_deleted_macros(self._db_path, printer_profile_id=self._active_printer_profile_id)

    def restore_version(
        self,
        file_path: str,
        macro_name: str,
        version: int,
    ) -> dict[str, object]:
        """Restore a historical macro version back into local cfg files."""
        if _cfg_is_protected(file_path):
            raise ValueError(self._protected_file_block_message(file_path))

        result = restore_macro_version(
            db_path=self._db_path,
            config_dir=self._resolve_runtime_config_dir(),
            file_path=file_path,
            macro_name=macro_name,
            version=version,
            printer_profile_id=self._active_printer_profile_id,
        )
        result["remote_synced"] = False
        result["local_changed"] = True
        return result

    def save_macro_editor_text(
        self,
        file_path: str,
        macro_name: str,
        section_text: str,
    ) -> dict[str, object]:
        """Save edited macro text back into its local cfg file."""
        if _cfg_is_protected(file_path):
            raise ValueError(self._protected_file_block_message(file_path))

        result = save_macro_edit(
            config_dir=self._resolve_runtime_config_dir(),
            file_path=file_path,
            macro_name=macro_name,
            section_text=section_text,
        )
        result["remote_synced"] = False
        result["local_changed"] = True
        return result

    def delete_macro_source(
        self,
        file_path: str,
        macro_name: str,
    ) -> dict[str, object]:
        """Delete one macro section from its local source cfg file."""
        if _cfg_is_protected(file_path):
            raise ValueError(self._protected_file_block_message(file_path))

        result = delete_macro_from_cfg(
            config_dir=self._resolve_runtime_config_dir(),
            file_path=file_path,
            macro_name=macro_name,
        )
        result["remote_synced"] = False
        result["local_changed"] = True
        return result

    def list_duplicates(self) -> list[dict[str, object]]:
        """Load duplicate macro groups used by resolution wizard."""
        return load_duplicate_macro_groups(self._db_path, printer_profile_id=self._active_printer_profile_id)

    def resolve_duplicates(
        self,
        keep_choices: dict[str, str],
        duplicate_groups: list[dict[str, object]],
    ) -> dict[str, object]:
        """Apply duplicate-resolution choices to local cfg files."""
        result = resolve_duplicate_macros(
            config_dir=self._resolve_runtime_config_dir(),
            keep_choices=keep_choices,
            duplicate_groups=duplicate_groups,
        )
        result["remote_synced"] = False
        result["local_changed"] = True
        return result

    def _latest_macro_file_counts_from_db(self) -> tuple[int, int]:
        """Return latest indexed macro counts for printer.cfg and macros.cfg."""
        printer_cfg_count = 0
        macros_cfg_count = 0
        with open_sqlite_connection(self._db_path) as conn:
            table_exists = bool(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='macros' LIMIT 1"
                ).fetchone()
            )
            if not table_exists:
                return 0, 0

            rows = conn.execute(
                """
                SELECT m.file_path, m.is_deleted
                FROM macros AS m
                INNER JOIN (
                    SELECT file_path, macro_name, MAX(version) AS max_version
                    FROM macros
                    WHERE printer_profile_id = ?
                    GROUP BY file_path, macro_name
                ) AS latest
                    ON m.file_path = latest.file_path
                   AND m.macro_name = latest.macro_name
                   AND m.version = latest.max_version
                WHERE m.printer_profile_id = ?
                """,
                (int(self._active_printer_profile_id), int(self._active_printer_profile_id)),
            ).fetchall()

        for file_path, is_deleted in rows:
            if bool(int(is_deleted)):
                continue
            base_name = Path(str(file_path or "")).name.lower()
            if base_name == "printer.cfg":
                printer_cfg_count += 1
            elif base_name == "macros.cfg":
                macros_cfg_count += 1
        return printer_cfg_count, macros_cfg_count

    def get_macro_migration_state(self) -> dict[str, object]:
        """Return migration readiness for printer.cfg -> macros.cfg move."""
        runtime_dir = self._resolve_runtime_config_dir()
        printer_cfg_path = runtime_dir / "printer.cfg"
        macros_cfg_path = runtime_dir / "macros.cfg"

        runtime_printer_cfg_count = 0
        if printer_cfg_path.exists():
            runtime_printer_cfg_count = len(parse_macros_from_cfg(printer_cfg_path, runtime_dir))

        runtime_macros_cfg_count = 0
        if macros_cfg_path.exists():
            runtime_macros_cfg_count = len(parse_macros_from_cfg(macros_cfg_path, runtime_dir))

        db_printer_cfg_count, db_macros_cfg_count = self._latest_macro_file_counts_from_db()

        printer_cfg_macro_count = runtime_printer_cfg_count if printer_cfg_path.exists() else db_printer_cfg_count
        macros_cfg_macro_count = runtime_macros_cfg_count if macros_cfg_path.exists() else db_macros_cfg_count
        macros_cfg_exists = bool(macros_cfg_path.exists() or macros_cfg_macro_count > 0)

        return {
            "printer_cfg_macro_count": int(printer_cfg_macro_count),
            "macros_cfg_macro_count": int(macros_cfg_macro_count),
            "macros_cfg_exists": macros_cfg_exists,
            # Migration stays available as long as printer.cfg still contains
            # macros that can be moved into macros.cfg.
            "can_migrate": printer_cfg_macro_count > 0,
        }

    def migrate_printer_cfg_macros(self, backup_name: str | None = None) -> dict[str, object]:
        """Create a backup, then move printer.cfg macros into macros.cfg."""
        migration_state = self.get_macro_migration_state()
        printer_cfg_macro_count = _as_int(migration_state.get("printer_cfg_macro_count", 0), default=0)
        if printer_cfg_macro_count <= 0:
            raise ValueError("No macros were found in printer.cfg.")

        resolved_backup_name = str(backup_name or "").strip()
        if not resolved_backup_name:
            resolved_backup_name = datetime.now().strftime("Macro_Migration-%Y%m%d-%H%M%S")

        backup_result = self.create_backup(resolved_backup_name)
        migration_result = migrate_printer_cfg_macros_to_macros_cfg(self._resolve_runtime_config_dir())

        return {
            "backup_name": backup_result.get("backup_name", resolved_backup_name),
            "backup_macro_count": backup_result.get("macro_count", 0),
            "moved_sections": migration_result.get("moved_sections", 0),
            "created_macros_cfg": bool(migration_result.get("created_macros_cfg", False)),
            "include_added": bool(migration_result.get("include_added", False)),
            "touched_files": migration_result.get("touched_files", []),
            "remote_synced": False,
            "local_changed": True,
        }

    @staticmethod
    def _require_non_empty(value: str, error_message: str) -> str:
        """Normalize a required text value and raise when empty."""
        normalized = _as_text(value)
        if not normalized:
            raise ValueError(error_message)
        return normalized

