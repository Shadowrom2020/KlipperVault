#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Service layer for KlipperVault GUI actions.

This module keeps database/config operations outside UI code so the NiceGUI
module can focus on rendering and user interactions.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, field_validator
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from klipper_macro_backup import (
    create_macro_backup,
    delete_macro_backup,
    list_macro_backups,
    load_backup_items,
    restore_macro_backup,
)
from klipper_macro_indexer import (
    delete_macro_from_cfg,
    export_macro_share_payload,
    import_macro_share_payload,
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
from klipper_macro_online_update import (
    check_online_macro_updates,
    import_online_macro_updates,
)
from klipper_macro_online_repo_export import (
    build_online_update_repository_artifacts,
    export_online_update_repository_zip,
)
from klipper_macro_github_api import (
    commit_changed_text_files,
    create_branch,
    create_pull_request,
    get_open_pull_request_for_head,
    load_json_file_from_branch,
)


def _as_int(value: object, default: int = 0) -> int:
    """Convert dynamic values to int with a safe fallback."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _as_list(value: object) -> list[object]:
    """Return list values unchanged and coerce everything else to an empty list."""
    return value if isinstance(value, list) else []


def _as_text(value: object) -> str:
    """Normalize dynamic values into stripped text, matching existing truthy/falsey behavior."""
    return str(value or "").strip()


class MoonrakerStatusResult(BaseModel):
    """Typed Moonraker printer status payload used internally by the service."""

    connected: bool
    state: str
    message: str
    is_printing: bool
    is_busy: bool

    @field_validator("state", mode="before")
    @classmethod
    def normalize_state(cls, v: object) -> str:
        """Normalize state to lowercase."""
        return _as_text(v).lower()

    @field_validator("message", mode="before")
    @classmethod
    def normalize_message(cls, v: object) -> str:
        """Normalize message (stripped but not lowercased)."""
        return _as_text(v)

    def as_dict(self) -> dict[str, object]:
        """Convert typed status payload to legacy dictionary contract."""
        return {
            "connected": self.connected,
            "state": self.state,
            "message": self.message,
            "is_printing": self.is_printing,
            "is_busy": self.is_busy,
        }


class MoonrakerCommandResult(BaseModel):
    """Typed Moonraker command response payload used internally by the service."""

    ok: bool
    status: int
    payload: dict[str, object]
    notification: str = ""

    @field_validator("status", mode="before")
    @classmethod
    def validate_status_code(cls, v: object) -> int:
        """Ensure status is a valid HTTP status code."""
        status = _as_int(v)
        if status < 100 or status > 999:
            raise ValueError("HTTP status code must be 100-999")
        return status

    @field_validator("notification", mode="before")
    @classmethod
    def normalize_notification(cls, v: object) -> str:
        """Normalize notification field."""
        return _as_text(v)

    def as_dict(self) -> dict[str, object]:
        """Convert typed command payload to legacy dictionary contract."""
        result: dict[str, object] = {
            "ok": self.ok,
            "status": self.status,
            "payload": self.payload,
        }
        if self.notification:
            result["notification"] = self.notification
        return result


class PullRequestCreationResult(BaseModel):
    """Typed pull request creation payload used internally by the service."""

    created: bool
    existing: bool
    pull_request_number: int
    pull_request_url: str
    head_branch: str
    updated_files: int
    macro_count: int
    no_changes: bool
    commit_count: int

    @field_validator("pull_request_number", "updated_files", "macro_count", "commit_count", mode="before")
    @classmethod
    def validate_non_negative_int(cls, v: object) -> int:
        """Ensure all count fields are non-negative integers."""
        value = _as_int(v)
        if value < 0:
            raise ValueError("count fields must be non-negative")
        return value

    @field_validator("pull_request_url", "head_branch", mode="before")
    @classmethod
    def normalize_url_and_branch(cls, v: object) -> str:
        """Normalize URL and branch name fields."""
        return _as_text(v)

    def as_dict(self) -> dict[str, object]:
        """Convert typed PR creation payload to legacy dictionary contract."""
        return {
            "created": self.created,
            "existing": self.existing,
            "pull_request_number": self.pull_request_number,
            "pull_request_url": self.pull_request_url,
            "head_branch": self.head_branch,
            "updated_files": self.updated_files,
            "macro_count": self.macro_count,
            "no_changes": self.no_changes,
            "commit_count": self.commit_count,
        }


class ImportedUpdateItem(BaseModel):
    """Typed imported update payload used for optional activation logic."""

    identity: str = ""
    file_path: str
    macro_name: str
    version: int

    @field_validator("file_path", "macro_name", mode="before")
    @classmethod
    def validate_required_fields(cls, v: object) -> str:
        """Ensure required fields are non-empty."""
        text = _as_text(v)
        if not text:
            raise ValueError("field must not be empty")
        return text

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, v: object) -> int:
        """Ensure version is a positive integer."""
        value = _as_int(v)
        if value <= 0:
            raise ValueError("version must be a positive integer")
        return value

    @field_validator("identity", mode="before")
    @classmethod
    def normalize_identity(cls, v: object) -> str:
        """Normalize identity field."""
        return _as_text(v)


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

    def _moonraker_url(self, path: str) -> str:
        """Build and validate a Moonraker URL for one API path."""
        parsed = urlparse(self._moonraker_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Moonraker URL must use http/https.")
        clean_path = path if path.startswith("/") else f"/{path}"
        return f"{self._moonraker_base_url}{clean_path}"

    @staticmethod
    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.2, min=0.2, max=1.5),
        retry=retry_if_exception_type(httpx.RequestError),
    )
    def _moonraker_get(
        url: str,
        *,
        params: dict[str, str] | None,
        timeout: float,
    ) -> httpx.Response:
        """Perform one Moonraker GET with bounded retry for transient transport errors."""
        return httpx.get(url, params=params, timeout=timeout)

    @staticmethod
    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.2, min=0.2, max=1.5),
        retry=retry_if_exception_type(httpx.RequestError),
    )
    def _moonraker_post(
        url: str,
        *,
        json_body: dict[str, str] | dict[str, object] | None,
        timeout: float,
    ) -> httpx.Response:
        """Perform one Moonraker POST with bounded retry for transient transport errors."""
        if json_body is None:
            return httpx.post(url, timeout=timeout)
        return httpx.post(url, json=json_body, timeout=timeout)

    @staticmethod
    def _decode_json_payload(response: httpx.Response) -> dict[str, object]:
        """Decode JSON response payload with a safe fallback to empty dict."""
        if not response.text:
            return {}
        try:
            decoded = response.json()
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}

    @staticmethod
    def _error_message_from_response(response: httpx.Response, payload: dict[str, object]) -> str:
        """Extract best-effort Moonraker error text from response payload/body."""
        payload_error = payload.get("error")
        if isinstance(payload_error, dict):
            message = str(payload_error.get("message", "")).strip()
            if message:
                return message
        body = response.text.strip()
        if body:
            return body
        return (response.reason_phrase or "").strip()

    @staticmethod
    def _status_result_from_payload(payload: dict[str, object]) -> MoonrakerStatusResult:
        """Normalize raw Moonraker status payload into typed status result."""
        result_block = payload.get("result")
        status_block = result_block.get("status") if isinstance(result_block, dict) else {}
        print_stats = status_block.get("print_stats") if isinstance(status_block, dict) else {}
        stats = print_stats if isinstance(print_stats, dict) else {}

        state = str(stats.get("state", "unknown")).strip().lower()
        message = str(stats.get("message", "")).strip()
        is_printing = state == "printing"
        is_busy = state not in {"standby", "ready", "complete", "cancelled"}
        return MoonrakerStatusResult(
            connected=True,
            state=state,
            message=message,
            is_printing=is_printing,
            is_busy=is_busy,
        )

    def _moonraker_post_command(
        self,
        *,
        path: str,
        timeout: float,
        json_body: dict[str, str] | dict[str, object] | None,
        error_prefix: str,
    ) -> MoonrakerCommandResult:
        """Execute one Moonraker POST command and normalize error handling."""
        url = self._moonraker_url(path)
        try:
            response = self._moonraker_post(url, json_body=json_body, timeout=timeout)
        except httpx.HTTPError as exc:
            raise RuntimeError(str(exc)) from exc

        payload = self._decode_json_payload(response)
        if response.status_code >= 400:
            error_message = self._error_message_from_response(response, payload)
            raise RuntimeError(error_message or f"{error_prefix} failed with status {response.status_code}")

        return MoonrakerCommandResult(
            ok=True,
            status=response.status_code,
            payload=payload,
        )

    def query_printer_status(self, timeout: float = 2.0) -> dict[str, object]:
        """Query Moonraker print stats and return normalized printer status."""
        try:
            url = self._moonraker_url("/printer/objects/query")
            response = self._moonraker_get(url, params={"print_stats": "state,message"}, timeout=timeout)
            payload = self._decode_json_payload(response)
        except (ValueError, httpx.HTTPError) as exc:
            return MoonrakerStatusResult(
                connected=False,
                state="unknown",
                message=str(exc),
                is_printing=False,
                is_busy=False,
            ).as_dict()

        if response.status_code >= 400:
            error_message = self._error_message_from_response(response, payload)
            return MoonrakerStatusResult(
                connected=False,
                state="unknown",
                message=error_message or f"Moonraker status request failed with status {response.status_code}",
                is_printing=False,
                is_busy=False,
            ).as_dict()

        return self._status_result_from_payload(payload).as_dict()

    def is_printer_printing(self, timeout: float = 2.0) -> bool:
        """Return True when Moonraker reports active printing."""
        status = self.query_printer_status(timeout=timeout)
        return bool(status.get("is_printing", False))

    def restart_klipper(self, timeout: float = 3.0) -> dict[str, object]:
        """Request a Klipper host restart through Moonraker."""
        result = self._moonraker_post_command(
            path="/printer/restart",
            timeout=timeout,
            json_body=None,
            error_prefix="Moonraker restart request",
        )
        return result.as_dict()

    def reload_dynamic_macros(self, timeout: float = 3.0) -> dict[str, object]:
        """Execute DYNAMIC_MACRO command through Moonraker gcode API."""
        result = self._moonraker_post_command(
            path="/printer/gcode/script",
            timeout=timeout,
            json_body={"script": "DYNAMIC_MACRO"},
            error_prefix="Moonraker dynamic reload request",
        )
        return result.as_dict()

    def send_mainsail_notification(
        self,
        *,
        message: str,
        title: str = "KlipperVault",
        timeout: float = 3.0,
    ) -> dict[str, object]:
        """Send a Mainsail frontend notification through Moonraker gcode script API."""
        clean_title = " ".join(str(title or "KlipperVault").split()).strip() or "KlipperVault"
        clean_message = " ".join(str(message or "").split()).strip()
        notification_text = f"{clean_title}: {clean_message}" if clean_message else clean_title
        escaped_notification = notification_text.replace("\\", "\\\\").replace('"', '\\"')
        gcode = f'RESPOND TYPE=command MSG="action:notification {escaped_notification}"'
        command_result = self._moonraker_post_command(
            path="/printer/gcode/script",
            timeout=timeout,
            json_body={"script": gcode},
            error_prefix="Moonraker notification request",
        )

        return MoonrakerCommandResult(
            ok=command_result.ok,
            status=command_result.status,
            payload=command_result.payload,
            notification=notification_text,
        ).as_dict()

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

    @staticmethod
    def _normalize_printer_identity(vendor: str, model: str) -> tuple[str, str]:
        """Normalize printer identity values for compatibility checks."""
        return _as_text(vendor).lower(), _as_text(model).lower()

    @staticmethod
    def _require_non_empty(value: str, error_message: str) -> str:
        """Normalize a required text value and raise when empty."""
        normalized = _as_text(value)
        if not normalized:
            raise ValueError(error_message)
        return normalized

    @staticmethod
    def _prepare_pr_artifacts(
        *,
        artifacts: dict[str, object],
        manifest_path: str,
    ) -> tuple[dict[str, str], list[str], int]:
        """Normalize repository artifacts into commit-ready write/delete collections."""
        files_to_write_raw = artifacts.get("files_to_write", {})
        if not isinstance(files_to_write_raw, dict):
            raise RuntimeError("invalid export payload generated for pull request")
        files_to_write: dict[str, str] = {
            str(path): str(content)
            for path, content in files_to_write_raw.items()
        }

        files_to_delete_raw = artifacts.get("files_to_delete", [])
        if not isinstance(files_to_delete_raw, list):
            raise RuntimeError("invalid delete payload generated for pull request")
        files_to_delete = [
            _as_text(path).lstrip("/")
            for path in files_to_delete_raw
            if _as_text(path)
        ]

        manifest_payload = artifacts.get("manifest", {})
        if not isinstance(manifest_payload, dict):
            raise RuntimeError("invalid manifest payload generated for pull request")
        files_to_write[manifest_path.lstrip("/")] = json.dumps(manifest_payload, indent=2, ensure_ascii=False)

        macro_count = _as_int(artifacts.get("macro_count", 0))
        return files_to_write, files_to_delete, macro_count

    @staticmethod
    def _parse_imported_update_item(item: object) -> ImportedUpdateItem | None:
        """Normalize one imported update item; return None for malformed entries."""
        if not isinstance(item, dict):
            return None

        try:
            return ImportedUpdateItem(**item)
        except Exception:
            return None

    def export_macro_share_file(
        self,
        identities: list[tuple[str, str]],
        source_vendor: str,
        source_model: str,
        out_file: Path,
    ) -> dict[str, object]:
        """Export selected latest macros to a shareable JSON file."""
        payload = export_macro_share_payload(
            db_path=self._db_path,
            identities=identities,
            source_vendor=source_vendor,
            source_model=source_model,
            now_ts=int(time.time()),
        )
        exported_macros = payload.get("macros", [])
        macro_count = len(exported_macros) if isinstance(exported_macros, list) else 0
        out_file = out_file.expanduser().resolve()
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return {
            "file_path": str(out_file),
            "macro_count": macro_count,
        }

    def import_macro_share_file(
        self,
        import_file: Path,
        target_vendor: str,
        target_model: str,
    ) -> dict[str, object]:
        """Import macros from a share file as new inactive rows."""
        import_file = import_file.expanduser().resolve()
        payload = json.loads(import_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("invalid macro share file")

        result = import_macro_share_payload(
            db_path=self._db_path,
            payload=payload,
            now_ts=int(time.time()),
        )

        source_vendor = _as_text(result.get("source_vendor", ""))
        source_model = _as_text(result.get("source_model", ""))
        src_vendor_norm, src_model_norm = self._normalize_printer_identity(source_vendor, source_model)
        tgt_vendor_norm, tgt_model_norm = self._normalize_printer_identity(target_vendor, target_model)
        printer_matches = bool(
            src_vendor_norm
            and src_model_norm
            and tgt_vendor_norm
            and tgt_model_norm
            and src_vendor_norm == tgt_vendor_norm
            and src_model_norm == tgt_model_norm
        )
        imported_count = _as_int(result.get("imported", 0))

        return {
            "imported": imported_count,
            "source_vendor": source_vendor,
            "source_model": source_model,
            "printer_matches": printer_matches,
            "file_path": str(import_file),
        }

    def check_online_updates(
        self,
        *,
        repo_url: str,
        manifest_path: str,
        repo_ref: str,
        source_vendor: str,
        source_model: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict[str, object]:
        """Check GitHub manifest for online macro updates for one printer identity."""
        return check_online_macro_updates(
            db_path=self._db_path,
            repo_url=repo_url,
            manifest_path=manifest_path,
            repo_ref=repo_ref,
            source_vendor=source_vendor,
            source_model=source_model,
            now_ts=int(time.time()),
            progress_callback=progress_callback,
        )

    def import_online_updates(
        self,
        *,
        updates: list[dict[str, object]],
        activate_identities: list[str],
        repo_url: str,
        repo_ref: str,
    ) -> dict[str, object]:
        """Import online updates and optionally activate selected imported versions."""
        import_result = import_online_macro_updates(
            db_path=self._db_path,
            updates=updates,
            repo_url=repo_url,
            repo_ref=repo_ref,
            now_ts=int(time.time()),
        )

        imported_items = _as_list(import_result.get("imported_items", []))
        activate_set = {_as_text(identity) for identity in activate_identities}
        activated_count = 0

        for item in imported_items:
            parsed_item = self._parse_imported_update_item(item)
            if parsed_item is None or parsed_item.identity not in activate_set:
                continue

            restore_macro_version(
                db_path=self._db_path,
                config_dir=self._config_dir,
                file_path=parsed_item.file_path,
                macro_name=parsed_item.macro_name,
                version=parsed_item.version,
            )
            activated_count += 1

        imported_count = _as_int(import_result.get("imported", 0))
        return {
            "imported": imported_count,
            "activated": activated_count,
            "imported_items": imported_items,
        }

    def export_online_update_repository_zip(
        self,
        *,
        out_file: Path,
        source_vendor: str,
        source_model: str,
        repo_url: str,
        repo_ref: str,
        manifest_path: str,
    ) -> dict[str, object]:
        """Export active local macros as a repository-ready online update zip."""
        return export_online_update_repository_zip(
            db_path=self._db_path,
            out_file=out_file,
            source_vendor=source_vendor,
            source_model=source_model,
            repo_url=repo_url,
            repo_ref=repo_ref,
            manifest_path=manifest_path,
            now_ts=int(time.time()),
        )

    def create_online_update_pull_request(
        self,
        *,
        source_vendor: str,
        source_model: str,
        repo_url: str,
        base_branch: str,
        head_branch: str,
        manifest_path: str,
        github_token: str,
        pull_request_title: str,
        pull_request_body: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict[str, object]:
        """Create a GitHub pull request with exported active macro artifacts."""
        clean_repo_url = self._require_non_empty(repo_url, "online update repository URL is required")
        clean_base = self._require_non_empty(base_branch, "base branch is required")
        clean_head = self._require_non_empty(head_branch, "head branch is required")
        clean_manifest_path = str(manifest_path or "updates/manifest.json").strip() or "updates/manifest.json"
        clean_token = self._require_non_empty(github_token, "GitHub token is required")
        clean_title = self._require_non_empty(pull_request_title, "pull request title is required")
        clean_body = str(pull_request_body or "").strip()

        def _report(current: int, total: int) -> None:
            if progress_callback is not None:
                progress_callback(max(int(current), 0), max(int(total), 1))

        _report(0, 6)

        existing_pr = get_open_pull_request_for_head(clean_repo_url, clean_token, clean_head)
        _report(1, 6)
        if existing_pr is not None:
            return PullRequestCreationResult(
                created=False,
                existing=True,
                pull_request_number=_as_int(existing_pr.get("number", 0)),
                pull_request_url=str(existing_pr.get("html_url", "")),
                head_branch=clean_head,
                updated_files=0,
                macro_count=0,
                no_changes=False,
                commit_count=0,
            ).as_dict()

        remote_manifest = load_json_file_from_branch(
            repo_url=clean_repo_url,
            token=clean_token,
            branch=clean_base,
            file_path=clean_manifest_path,
        )
        _report(2, 6)

        artifacts = build_online_update_repository_artifacts(
            db_path=self._db_path,
            source_vendor=source_vendor,
            source_model=source_model,
            manifest_path=clean_manifest_path,
            now_ts=int(time.time()),
            existing_manifest=remote_manifest,
        )
        _report(3, 6)
        files_to_write, files_to_delete, macro_count = self._prepare_pr_artifacts(
            artifacts=artifacts,
            manifest_path=clean_manifest_path,
        )

        branch_result = create_branch(
            repo_url=clean_repo_url,
            token=clean_token,
            base_branch=clean_base,
            head_branch=clean_head,
        )
        _report(4, 6)
        if bool(branch_result.get("already_exists", False)):
            raise RuntimeError(
                "head branch already exists on remote repository; choose a different branch name"
            )

        def _map_commit_progress(current: int, total: int) -> None:
            safe_total = max(int(total), 1)
            safe_current = max(int(current), 0)
            scaled_current = 4000 + int((safe_current / safe_total) * 1000)
            _report(scaled_current, 6000)

        commit_result = commit_changed_text_files(
            repo_url=clean_repo_url,
            token=clean_token,
            branch=clean_head,
            files=files_to_write,
            deleted_files=files_to_delete,
            commit_message=f"Update macros for {source_vendor} {source_model}",
            progress_callback=_map_commit_progress,
        )
        updated_files = _as_int(commit_result.get("changed_files", 0))

        if updated_files <= 0:
            _report(6, 6)
            return PullRequestCreationResult(
                created=False,
                existing=False,
                pull_request_number=0,
                pull_request_url="",
                head_branch=clean_head,
                updated_files=0,
                macro_count=macro_count,
                no_changes=True,
                commit_count=0,
            ).as_dict()

        pull_request_result = create_pull_request(
            repo_url=clean_repo_url,
            token=clean_token,
            base_branch=clean_base,
            head_branch=clean_head,
            title=clean_title,
            body=clean_body,
        )
        _report(6, 6)

        return PullRequestCreationResult(
            created=True,
            existing=bool(pull_request_result.get("existing", False)),
            pull_request_number=_as_int(pull_request_result.get("number", 0)),
            pull_request_url=str(pull_request_result.get("url", "")),
            head_branch=clean_head,
            updated_files=updated_files,
            macro_count=macro_count,
            no_changes=False,
            commit_count=1,
        ).as_dict()
