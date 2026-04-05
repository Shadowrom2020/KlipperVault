#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Service layer for KlipperVault GUI actions.

This module keeps database/config operations outside UI code so the NiceGUI
module can focus on rendering and user interactions.
"""

from __future__ import annotations

import http.client
import json
from pathlib import Path
from urllib.parse import urlencode, urlparse

from klipper_macro_backup import (
    create_macro_backup,
    delete_macro_backup,
    list_macro_backups,
    load_backup_items,
    restore_macro_backup,
)
from klipper_macro_indexer import (
    delete_macro_from_cfg,
    load_duplicate_macro_groups,
    load_macro_list,
    load_macro_versions,
    load_stats,
    macro_row_to_section_text,
    remove_all_deleted_macros,
    remove_deleted_macro,
    remove_inactive_macro_version,
    restore_macro_version,
    resolve_duplicate_macros,
    run_indexing,
    save_macro_edit,
)


class MacroGuiService:
    """Coordinates backend operations used by the GUI layer."""

    def __init__(
        self,
        db_path: Path,
        config_dir: Path,
        version_history_size: int,
        moonraker_base_url: str = "http://127.0.0.1:7125",
    ) -> None:
        self._db_path = db_path
        self._config_dir = config_dir
        self._version_history_size = version_history_size
        self._moonraker_base_url = moonraker_base_url.rstrip("/")

    def query_printer_status(self, timeout: float = 2.0) -> dict[str, object]:
        """Query Moonraker print stats and return normalized printer status."""
        query = urlencode({"print_stats": "state,message"})
        url = f"{self._moonraker_base_url}/printer/objects/query?{query}"
        parsed = urlparse(url)

        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return {
                "connected": False,
                "state": "unknown",
                "message": "Moonraker URL must use http/https.",
                "is_printing": False,
            }

        try:
            connection: http.client.HTTPConnection | http.client.HTTPSConnection
            if parsed.scheme == "https":
                connection = http.client.HTTPSConnection(parsed.netloc, timeout=timeout)
            else:
                connection = http.client.HTTPConnection(parsed.netloc, timeout=timeout)
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            connection.request("GET", path)
            response = connection.getresponse()
            raw_payload = response.read().decode("utf-8")
            connection.close()
            payload = json.loads(raw_payload)
        except (OSError, TimeoutError, ValueError, http.client.HTTPException) as exc:
            return {
                "connected": False,
                "state": "unknown",
                "message": str(exc),
                "is_printing": False,
            }

        status_block = payload.get("result", {}).get("status", {})
        print_stats = status_block.get("print_stats", {})
        state = str(print_stats.get("state", "unknown")).strip().lower()
        message = str(print_stats.get("message", "")).strip()
        is_printing = state == "printing"
        is_busy = state not in {"standby", "ready", "complete", "cancelled"}
        return {
            "connected": True,
            "state": state,
            "message": message,
            "is_printing": is_printing,
            "is_busy": is_busy,
        }

    def is_printer_printing(self, timeout: float = 2.0) -> bool:
        """Return True when Moonraker reports active printing."""
        status = self.query_printer_status(timeout=timeout)
        return bool(status.get("is_printing", False))

    def restart_klipper(self, timeout: float = 3.0) -> dict[str, object]:
        """Request a Klipper host restart through Moonraker."""
        url = f"{self._moonraker_base_url}/printer/restart"
        parsed = urlparse(url)

        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Moonraker URL must use http/https.")

        connection: http.client.HTTPConnection | http.client.HTTPSConnection
        if parsed.scheme == "https":
            connection = http.client.HTTPSConnection(parsed.netloc, timeout=timeout)
        else:
            connection = http.client.HTTPConnection(parsed.netloc, timeout=timeout)

        try:
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            connection.request("POST", path, body="", headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            raw_payload = response.read().decode("utf-8")
        finally:
            connection.close()

        try:
            payload = json.loads(raw_payload) if raw_payload else {}
        except ValueError:
            payload = {}

        if response.status >= 400:
            error_message = str(payload.get("error", {}).get("message") or raw_payload or response.reason).strip()
            raise RuntimeError(error_message or f"Moonraker restart request failed with status {response.status}")

        return {
            "ok": True,
            "status": response.status,
            "payload": payload,
        }

    def index(self) -> dict[str, object]:
        """Run config indexing with configured retention settings."""
        return run_indexing(
            config_dir=self._config_dir,
            db_path=self._db_path,
            max_versions=self._version_history_size,
        )

    def load_dashboard(self) -> tuple[dict[str, object], list[dict[str, object]]]:
        """Load aggregate stats and latest macro list for dashboard refresh."""
        return load_stats(self._db_path), load_macro_list(self._db_path)

    def load_versions(self, file_path: str, macro_name: str) -> list[dict[str, object]]:
        """Load version history for a specific macro identity."""
        return load_macro_versions(self._db_path, file_path, macro_name)

    def load_latest_for_file(self, macro_name: str, file_path: str) -> dict[str, object] | None:
        """Load latest stored row for one macro definition file."""
        versions = self.load_versions(file_path, macro_name)
        return versions[0] if versions else None

    def build_macro_section_text(self, macro: dict[str, object]) -> str:
        """Build editable cfg section text for one macro row."""
        return macro_row_to_section_text(macro)

    def remove_deleted(self, file_path: str, macro_name: str) -> dict[str, object]:
        """Permanently remove a deleted macro history from database."""
        return remove_deleted_macro(self._db_path, file_path, macro_name)

    def remove_inactive_version(self, file_path: str, macro_name: str, version: int) -> dict[str, object]:
        """Permanently remove one inactive macro version from database."""
        return remove_inactive_macro_version(self._db_path, file_path, macro_name, version)

    def purge_all_deleted(self) -> dict[str, object]:
        """Remove all deleted macro histories from database."""
        return remove_all_deleted_macros(self._db_path)

    def restore_version(self, file_path: str, macro_name: str, version: int) -> dict[str, object]:
        """Restore a historical macro version back into cfg files."""
        return restore_macro_version(
            db_path=self._db_path,
            config_dir=self._config_dir,
            file_path=file_path,
            macro_name=macro_name,
            version=version,
        )

    def save_macro_editor_text(self, file_path: str, macro_name: str, section_text: str) -> dict[str, object]:
        """Save edited macro text back into its cfg file."""
        return save_macro_edit(
            config_dir=self._config_dir,
            file_path=file_path,
            macro_name=macro_name,
            section_text=section_text,
        )

    def delete_macro_source(self, file_path: str, macro_name: str) -> dict[str, object]:
        """Delete one macro section from its source cfg file."""
        return delete_macro_from_cfg(
            config_dir=self._config_dir,
            file_path=file_path,
            macro_name=macro_name,
        )

    def list_duplicates(self) -> list[dict[str, object]]:
        """Load duplicate macro groups used by resolution wizard."""
        return load_duplicate_macro_groups(self._db_path)

    def resolve_duplicates(
        self,
        keep_choices: dict[str, str],
        duplicate_groups: list[dict[str, object]],
    ) -> dict[str, object]:
        """Apply duplicate-resolution choices to cfg files."""
        return resolve_duplicate_macros(
            config_dir=self._config_dir,
            keep_choices=keep_choices,
            duplicate_groups=duplicate_groups,
        )

    def create_backup(self, name: str) -> dict[str, object]:
        """Create a named backup snapshot from current macro state."""
        return create_macro_backup(
            db_path=self._db_path,
            backup_name=name,
            config_dir=self._config_dir,
        )

    def list_backups(self) -> list[dict[str, object]]:
        """Return all available backups."""
        return list_macro_backups(self._db_path)

    def load_backup_contents(self, backup_id: int) -> list[dict[str, object]]:
        """Return snapshot items for one backup."""
        return load_backup_items(self._db_path, backup_id)

    def restore_backup(self, backup_id: int) -> dict[str, object]:
        """Restore selected backup state to db/cfg."""
        return restore_macro_backup(
            db_path=self._db_path,
            backup_id=backup_id,
            config_dir=self._config_dir,
        )

    def delete_backup(self, backup_id: int) -> dict[str, object]:
        """Delete one backup snapshot."""
        return delete_macro_backup(db_path=self._db_path, backup_id=backup_id)
