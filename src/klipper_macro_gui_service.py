#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Service layer for KlipperVault GUI actions.

This module keeps database/config operations outside UI code so the NiceGUI
module can focus on rendering and user interactions.
"""

from __future__ import annotations

import json
import hashlib
import shutil
import tempfile
import atexit
import time
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ValidationError, field_validator
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
    get_cfg_loading_overview,
    get_cfg_loading_overview_from_source,
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
    run_indexing_from_source,
    save_macro_edit,
)
from klipper_macro_online_update import (
    check_online_macro_updates,
    import_online_macro_updates,
)
from klipper_vault_config_source import LocalConfigSource, SshConfigSource
from klipper_vault_db import open_sqlite_connection
from klipper_vault_remote_profiles import (
    delete_ssh_host_profile,
    SshHostProfile,
    ensure_remote_profile_schema,
    get_active_ssh_host_profile,
    get_credential_backend,
    list_ssh_host_profiles,
    set_active_ssh_host_profile,
    upsert_ssh_host_profile,
)
from klipper_vault_secret_store import CredentialStore
from klipper_vault_ssh_transport import SshConnectionConfig, SshTransport
from klipper_vault_printer_profiles import (
    create_printer_profile,
    ensure_default_printer_profile,
    ensure_printer_profile_schema,
    get_active_printer_profile,
    get_printer_profile_by_ssh_profile_id,
    list_printer_profiles,
    set_active_printer_profile,
    update_printer_profile_connection,
    update_printer_profile_identity,
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
from klipper_type_utils import to_int as _as_int
from klipper_type_utils import to_text as _as_text


_PROTECTED_CFG_FILENAME = "printer.cfg"


def _cfg_is_protected(file_path: str) -> bool:
    """Return True when cfg path points to protected printer.cfg."""
    return Path(_as_text(file_path)).name.lower() == _PROTECTED_CFG_FILENAME


def _as_list(value: object) -> list[object]:
    """Return list values unchanged and coerce everything else to an empty list."""
    return value if isinstance(value, list) else []


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
        runtime_mode: str = "off_printer",
        moonraker_base_url: str = "",
    ) -> None:
        self._db_path = db_path
        self._config_dir = config_dir
        self._version_history_size = version_history_size
        self._runtime_mode = "off_printer"
        self._moonraker_base_url = moonraker_base_url.rstrip("/")
        self._cache_base_dir = Path(tempfile.gettempdir()) / "klippervault"
        self._active_cache_dir: Path | None = None
        self._active_cache_printer_profile_id: int | None = None
        self._remote_cfg_checksums: dict[str, str] = {}
        self._credential_store = CredentialStore(self._db_path)
        atexit.register(self.cleanup_runtime_cache)
        # Prepare off-printer profile/credential tables before SSH features are wired into UI.
        with open_sqlite_connection(self._db_path, ensure_schema=ensure_remote_profile_schema) as conn:
            conn.commit()
        with open_sqlite_connection(self._db_path, ensure_schema=ensure_printer_profile_schema) as conn:
            conn.commit()
        self._active_printer_profile_id = int(ensure_default_printer_profile(self._db_path))

        # In off-printer mode the active SSH profile is the source of Moonraker routing.
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
        """Remove active off-printer cache directory when available."""
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

    def list_printer_profiles(self) -> list[dict[str, object]]:
        """Return configured printer profiles."""
        payloads: list[dict[str, object]] = []
        for profile in list_printer_profiles(self._db_path):
            payloads.append(
                {
                    "id": profile.id,
                    "profile_name": profile.profile_name,
                    "vendor": profile.vendor,
                    "model": profile.model,
                    "connection_type": profile.connection_type,
                    "ssh_host": profile.ssh_host,
                    "ssh_port": profile.ssh_port,
                    "ssh_username": profile.ssh_username,
                    "ssh_remote_config_dir": profile.ssh_remote_config_dir,
                    "ssh_moonraker_url": profile.ssh_moonraker_url,
                    "ssh_auth_mode": profile.ssh_auth_mode,
                    "ssh_credential_ref": profile.ssh_credential_ref,
                    "ssh_profile_id": profile.ssh_profile_id,
                    "is_active": profile.is_active,
                    "is_archived": profile.is_archived,
                }
            )
        return payloads

    def get_active_printer_profile(self) -> dict[str, object] | None:
        """Return active printer profile metadata."""
        profile = get_active_printer_profile(self._db_path)
        if profile is None:
            return None
        return {
            "id": profile.id,
            "profile_name": profile.profile_name,
            "vendor": profile.vendor,
            "model": profile.model,
            "connection_type": profile.connection_type,
            "ssh_host": profile.ssh_host,
            "ssh_port": profile.ssh_port,
            "ssh_username": profile.ssh_username,
            "ssh_remote_config_dir": profile.ssh_remote_config_dir,
            "ssh_moonraker_url": profile.ssh_moonraker_url,
            "ssh_auth_mode": profile.ssh_auth_mode,
            "ssh_credential_ref": profile.ssh_credential_ref,
            "ssh_profile_id": profile.ssh_profile_id,
            "is_active": profile.is_active,
            "is_archived": profile.is_archived,
        }

    def activate_printer_profile(self, profile_id: int) -> dict[str, object]:
        """Activate one printer profile and refresh service routing state."""
        updated = set_active_printer_profile(self._db_path, int(profile_id))
        if not updated:
            return {"ok": False, "error": "profile not found"}
        self.cleanup_runtime_cache()
        self._active_printer_profile_id = int(profile_id)
        profile = get_active_printer_profile(self._db_path)
        if profile is not None and profile.ssh_profile_id is not None:
            set_active_ssh_host_profile(self._db_path, int(profile.ssh_profile_id))
        self._refresh_moonraker_base_url_from_active_profile()
        return {"ok": True, "profile_id": int(profile_id)}

    def ensure_printer_profile_for_ssh_profile(
        self,
        *,
        ssh_profile_id: int,
        profile_name: str,
        activate: bool = True,
    ) -> dict[str, object]:
        """Ensure one printer profile exists for a given SSH profile relation."""
        existing = get_printer_profile_by_ssh_profile_id(self._db_path, int(ssh_profile_id))
        if existing is not None and existing.id is not None:
            profile_id = int(existing.id)
            if activate:
                self.activate_printer_profile(profile_id)
            return {"ok": True, "profile_id": profile_id, "created": False}

        # Backward-compatible fallback: match by profile name before creating a duplicate row.
        normalized_name = _as_text(profile_name)
        if normalized_name:
            for candidate in list_printer_profiles(self._db_path):
                if _as_text(candidate.profile_name) == normalized_name and candidate.id is not None:
                    profile_id = int(candidate.id)
                    if activate:
                        self.activate_printer_profile(profile_id)
                    return {"ok": True, "profile_id": profile_id, "created": False}

        created_profile_id = create_printer_profile(
            self._db_path,
            profile_name=profile_name,
            connection_type="off_printer",
            ssh_profile_id=int(ssh_profile_id),
            is_active=bool(activate),
        )
        if activate:
            self._active_printer_profile_id = int(created_profile_id)
        return {"ok": True, "profile_id": int(created_profile_id), "created": True}

    def _find_ssh_profile(self, profile_id: int) -> SshHostProfile | None:
        """Return one SSH profile by id from current storage snapshot."""
        target_id = int(profile_id)
        for profile in list_ssh_host_profiles(self._db_path):
            if profile.id is not None and int(profile.id) == target_id:
                return profile
        return None

    def update_active_printer_identity(self, vendor: str, model: str) -> dict[str, object]:
        """Update vendor/model for current active printer profile."""
        profile = get_active_printer_profile(self._db_path)
        if profile is None or profile.id is None:
            return {"ok": False, "error": "active profile not found"}
        updated = update_printer_profile_identity(
            self._db_path,
            profile_id=int(profile.id),
            vendor=vendor,
            model=model,
        )
        if not updated:
            return {"ok": False, "error": "profile not updated"}
        return {"ok": True, "profile_id": int(profile.id)}

    def _refresh_moonraker_base_url_from_active_profile(self) -> None:
        """Refresh Moonraker base URL from active off-printer profile when available."""
        active_printer = get_active_printer_profile(self._db_path)
        profile_moonraker_url = _as_text(active_printer.ssh_moonraker_url) if active_printer is not None else ""
        profile_ssh_host = _as_text(active_printer.ssh_host) if active_printer is not None else ""
        if not profile_moonraker_url:
            active_profile = get_active_ssh_host_profile(self._db_path)
            if active_profile is None:
                return
            profile_moonraker_url = _as_text(active_profile.moonraker_url)
            if not profile_ssh_host:
                profile_ssh_host = _as_text(active_profile.host)
        if profile_moonraker_url:
            normalized = self._normalize_off_printer_moonraker_url(profile_moonraker_url, profile_ssh_host)
            self._moonraker_base_url = normalized.rstrip("/")

    @staticmethod
    def _normalize_off_printer_moonraker_url(moonraker_url: str, ssh_host: str) -> str:
        """Rewrite localhost Moonraker URLs to the remote SSH host for off-printer mode."""
        raw_url = _as_text(moonraker_url)
        remote_host = _as_text(ssh_host)
        if not raw_url:
            return raw_url

        parse_target = raw_url if "://" in raw_url else f"http://{raw_url}"
        parsed = urlparse(parse_target)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return raw_url

        current_host = _as_text(parsed.hostname).lower()
        if current_host not in {"localhost", "127.0.0.1", "::1"} or not remote_host:
            return parsed.geturl()

        netloc_host = remote_host
        if ":" in remote_host and not remote_host.startswith("["):
            netloc_host = f"[{remote_host}]"

        userinfo = ""
        if parsed.username:
            userinfo = parsed.username
            if parsed.password:
                userinfo += f":{parsed.password}"
            userinfo += "@"

        netloc = f"{userinfo}{netloc_host}"
        if parsed.port is not None:
            netloc += f":{parsed.port}"
        return parsed._replace(netloc=netloc).geturl()

    @staticmethod
    def _normalize_remote_root(remote_config_dir: str, username: str) -> PurePosixPath:
        """Normalize remote config root and expand leading ~/ using profile username."""
        raw_root = _as_text(remote_config_dir).strip()
        user = _as_text(username).strip()
        if raw_root == "~":
            raw_root = f"/home/{user}" if user else "/"
        elif raw_root.startswith("~/"):
            raw_root = f"/home/{user}/{raw_root[2:]}" if user else f"/{raw_root[2:]}"
        return PurePosixPath(raw_root or "/")

    @staticmethod
    def _profile_to_dict(profile: SshHostProfile) -> dict[str, object]:
        """Convert profile model to UI-safe payload (without secrets)."""
        return {
            "id": profile.id,
            "profile_name": profile.profile_name,
            "host": profile.host,
            "port": profile.port,
            "username": profile.username,
            "remote_config_dir": profile.remote_config_dir,
            "moonraker_url": profile.moonraker_url,
            "auth_mode": profile.auth_mode,
            "credential_ref": profile.credential_ref,
            "is_active": profile.is_active,
        }

    @staticmethod
    def _printer_profile_to_ssh_payload(profile: dict[str, object]) -> dict[str, object]:
        """Convert active printer profile payload into legacy SSH profile API shape."""
        return {
            "id": _as_int(profile.get("id"), default=0),
            "profile_name": _as_text(profile.get("profile_name", "")),
            "host": _as_text(profile.get("ssh_host", "")),
            "port": _as_int(profile.get("ssh_port", 22), default=22),
            "username": _as_text(profile.get("ssh_username", "")),
            "remote_config_dir": _as_text(profile.get("ssh_remote_config_dir", "")),
            "moonraker_url": _as_text(profile.get("ssh_moonraker_url", "")),
            "auth_mode": _as_text(profile.get("ssh_auth_mode", "key")) or "key",
            "credential_ref": _as_text(profile.get("ssh_credential_ref", "")),
            "ssh_profile_id": profile.get("ssh_profile_id"),
            "is_active": bool(profile.get("is_active", False)),
        }

    @staticmethod
    def _normalize_credential_ref(profile_name: str, auth_mode: str) -> str:
        """Build a stable credential reference for profile secrets."""
        cleaned_name = "-".join(_as_text(profile_name).lower().split()) or "default"
        cleaned_auth_mode = _as_text(auth_mode).lower() or "key"
        return f"ssh:{cleaned_name}:{cleaned_auth_mode}"

    def list_ssh_profiles(self) -> list[dict[str, object]]:
        """Return off-printer connection profiles (owned by printer profiles)."""
        payloads: list[dict[str, object]] = []
        for printer_profile in self.list_printer_profiles():
            if _as_text(printer_profile.get("connection_type", "")) != "off_printer":
                continue
            if not _as_text(printer_profile.get("ssh_host", "")):
                continue
            payload = self._printer_profile_to_ssh_payload(printer_profile)
            credential_ref = _as_text(payload.get("credential_ref", ""))
            backend = get_credential_backend(self._db_path, credential_ref) if credential_ref else None
            payload["secret_backend"] = backend or ""
            payload["has_secret"] = bool(self._credential_store.get_secret(credential_ref=credential_ref))
            payloads.append(payload)
        return payloads

    def get_active_ssh_profile(self) -> dict[str, object] | None:
        """Return active off-printer connection settings from active printer profile."""
        active_printer = self.get_active_printer_profile()
        if isinstance(active_printer, dict) and active_printer:
            payload = self._printer_profile_to_ssh_payload(active_printer)
            if _as_text(payload.get("host", "")):
                credential_ref = _as_text(payload.get("credential_ref", ""))
                backend = get_credential_backend(self._db_path, credential_ref) if credential_ref else None
                payload["secret_backend"] = backend or ""
                payload["has_secret"] = bool(self._credential_store.get_secret(credential_ref=credential_ref))
                return payload

        # Legacy fallback while older profile rows are still in use.
        profile = get_active_ssh_host_profile(self._db_path)
        if profile is None:
            return None
        payload = self._profile_to_dict(profile)
        backend = get_credential_backend(self._db_path, profile.credential_ref) if profile.credential_ref else None
        payload["secret_backend"] = backend or ""
        payload["has_secret"] = bool(self._credential_store.get_secret(credential_ref=profile.credential_ref or ""))
        return payload

    def save_ssh_profile(
        self,
        *,
        profile_name: str,
        host: str,
        username: str,
        remote_config_dir: str,
        moonraker_url: str,
        port: int = 22,
        auth_mode: str = "key",
        is_active: bool = False,
        credential_ref: str = "",
        secret_value: str | None = None,
    ) -> dict[str, object]:
        """Create or update one SSH host profile and optionally persist secret material."""
        profile_name = self._require_non_empty(profile_name, "profile_name is required")
        host = self._require_non_empty(host, "host is required")
        username = self._require_non_empty(username, "username is required")
        remote_config_dir = self._require_non_empty(remote_config_dir, "remote_config_dir is required")
        moonraker_url = self._require_non_empty(moonraker_url, "moonraker_url is required")
        moonraker_url = self._normalize_off_printer_moonraker_url(moonraker_url, host)
        auth_mode = (_as_text(auth_mode).lower() or "key")
        if auth_mode not in {"key", "password"}:
            raise ValueError("auth_mode must be 'key' or 'password'")

        normalized_credential_ref = _as_text(credential_ref) or self._normalize_credential_ref(profile_name, auth_mode)
        credential_backend_name = ""
        if secret_value is not None:
            credential_backend_name = self._credential_store.set_secret(
                credential_ref=normalized_credential_ref,
                secret_type=auth_mode,
                secret_value=str(secret_value),
            )

        legacy_ssh_profile_id = upsert_ssh_host_profile(
            self._db_path,
            SshHostProfile(
                profile_name=profile_name,
                host=host,
                port=max(1, min(65535, int(port))),
                username=username,
                remote_config_dir=remote_config_dir,
                moonraker_url=moonraker_url,
                auth_mode=auth_mode,
                credential_ref=normalized_credential_ref,
                is_active=bool(is_active),
            ),
        )

        if not credential_backend_name and normalized_credential_ref:
            credential_backend_name = get_credential_backend(self._db_path, normalized_credential_ref) or ""

        ensure_result = self.ensure_printer_profile_for_ssh_profile(
            ssh_profile_id=int(legacy_ssh_profile_id),
            profile_name=profile_name,
            activate=bool(is_active),
        )
        printer_profile_id = _as_int(ensure_result.get("profile_id"), default=0)
        if printer_profile_id <= 0:
            raise RuntimeError("Failed to create or resolve printer profile")

        update_printer_profile_connection(
            self._db_path,
            profile_id=printer_profile_id,
            host=host,
            port=max(1, min(65535, int(port))),
            username=username,
            remote_config_dir=remote_config_dir,
            moonraker_url=moonraker_url,
            auth_mode=auth_mode,
            credential_ref=normalized_credential_ref,
            ssh_profile_id=int(legacy_ssh_profile_id),
        )

        if bool(is_active):
            self.activate_printer_profile(printer_profile_id)
            set_active_ssh_host_profile(self._db_path, int(legacy_ssh_profile_id))
            self._refresh_moonraker_base_url_from_active_profile()

        return {
            "ok": True,
            "profile_id": int(legacy_ssh_profile_id),
            "printer_profile_id": printer_profile_id,
            "legacy_ssh_profile_id": int(legacy_ssh_profile_id),
            "credential_ref": normalized_credential_ref,
            "secret_backend": credential_backend_name,
            "is_active": bool(is_active),
        }

    def activate_ssh_profile(self, profile_id: int) -> dict[str, object]:
        """Activate one saved off-printer connection profile."""
        linked_profile = get_printer_profile_by_ssh_profile_id(self._db_path, int(profile_id))
        if linked_profile is not None and linked_profile.id is not None:
            resolved_printer_profile_id = int(linked_profile.id)
        else:
            resolved_printer_profile_id = int(profile_id)

        updated = set_active_printer_profile(self._db_path, resolved_printer_profile_id)
        if not updated:
            return {"ok": False, "error": "profile not found"}

        self.cleanup_runtime_cache()
        self._active_printer_profile_id = resolved_printer_profile_id
        profile = get_active_printer_profile(self._db_path)
        if profile is not None and profile.ssh_profile_id is not None:
            set_active_ssh_host_profile(self._db_path, int(profile.ssh_profile_id))

        self._refresh_moonraker_base_url_from_active_profile()
        return {"ok": True, "profile_id": resolved_printer_profile_id}

    def delete_ssh_profile(self, profile_id: int) -> dict[str, object]:
        """Delete one saved printer-owned connection profile and clean secret material."""
        profile = next(
            (
                p
                for p in self.list_printer_profiles()
                if _as_int(p.get("ssh_profile_id"), default=0) == int(profile_id)
            ),
            None,
        )
        if not isinstance(profile, dict):
            profile = next(
                (
                    p
                    for p in self.list_printer_profiles()
                    if _as_int(p.get("id"), default=0) == int(profile_id)
                ),
                None,
            )
        if not isinstance(profile, dict):
            return {"ok": False, "error": "profile not found"}

        credential_ref = _as_text(profile.get("ssh_credential_ref", ""))
        if credential_ref:
            self._credential_store.delete_secret(credential_ref=credential_ref)

        now = int(time.time())
        with open_sqlite_connection(self._db_path, ensure_schema=ensure_printer_profile_schema) as conn:
            conn.execute(
                """
                UPDATE printer_profiles
                SET is_active = 0,
                    is_archived = 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, _as_int(profile.get("id"), default=0)),
            )
            conn.commit()

        legacy_ssh_profile_id = profile.get("ssh_profile_id")
        legacy_ssh_profile_id_int = _as_int(legacy_ssh_profile_id, default=0)
        if legacy_ssh_profile_id_int > 0:
            delete_ssh_host_profile(self._db_path, legacy_ssh_profile_id_int)

        if bool(profile.get("is_active", False)):
            self.cleanup_runtime_cache()

        return {
            "ok": True,
            "profile_id": _as_int(profile.get("id"), default=0),
            "was_active": bool(profile.get("is_active", False)),
        }

    def resolve_ssh_secret(self, credential_ref: str) -> dict[str, object]:
        """Resolve one credential reference to availability metadata for SSH usage."""
        normalized_ref = _as_text(credential_ref)
        if not normalized_ref:
            return {"ok": False, "error": "credential_ref is required"}
        secret_value = self._credential_store.get_secret(credential_ref=normalized_ref)
        return {
            "ok": True,
            "credential_ref": normalized_ref,
            "secret_backend": get_credential_backend(self._db_path, normalized_ref) or "",
            "has_secret": bool(secret_value),
            # Internal callers can use this field; UI code should avoid logging it.
            "secret_value": secret_value or "",
        }

    def _active_ssh_transport(self) -> tuple[SshTransport, dict[str, object]]:
        """Build SSH transport from currently active profile and stored credentials."""
        active_profile = self.get_active_ssh_profile()
        if active_profile is None:
            raise RuntimeError("No active SSH profile configured")

        credential_ref = _as_text(active_profile.get("credential_ref", ""))
        if not credential_ref:
            raise RuntimeError("Active SSH profile has no credential reference")

        resolved_secret = self.resolve_ssh_secret(credential_ref)
        if not bool(resolved_secret.get("has_secret", False)):
            raise RuntimeError("Active SSH profile is missing credentials")

        transport = SshTransport(
            SshConnectionConfig(
                host=_as_text(active_profile.get("host", "")),
                port=_as_int(active_profile.get("port", 22), default=22),
                username=_as_text(active_profile.get("username", "")),
                auth_mode=_as_text(active_profile.get("auth_mode", "key")),
                secret_value=_as_text(resolved_secret.get("secret_value", "")),
            )
        )
        return transport, active_profile

    def _runtime_local_config_source(self) -> LocalConfigSource:
        """Return local config source for the current runtime cache directory."""
        runtime_config_dir = self._resolve_runtime_config_dir()
        runtime_config_dir.mkdir(parents=True, exist_ok=True)
        return LocalConfigSource(root_dir=runtime_config_dir)

    def _active_remote_config_source(self) -> tuple[SshConfigSource, dict[str, object]]:
        """Return SSH-backed config source for the active remote profile."""
        transport, profile = self._active_ssh_transport()
        remote_config_dir = _as_text(profile.get("remote_config_dir", ""))
        if not remote_config_dir:
            raise RuntimeError("Active SSH profile is missing remote config directory")
        return SshConfigSource(transport=transport, remote_root=remote_config_dir), profile

    def test_active_ssh_connection(self) -> dict[str, object]:
        """Validate active off-printer SSH profile connectivity."""
        transport, profile = self._active_ssh_transport()
        result = transport.test_connection()
        return {
            "ok": bool(result.get("ok", False)),
            "profile_name": _as_text(profile.get("profile_name", "")),
            "host": _as_text(profile.get("host", "")),
            "elapsed_ms": _as_int(result.get("elapsed_ms", 0), default=0),
            "error": _as_text(result.get("error", "")),
            "output": _as_text(result.get("output", "")),
        }

    def list_active_remote_cfg_files(self) -> dict[str, object]:
        """List remote .cfg files for active SSH profile."""
        transport, profile = self._active_ssh_transport()
        remote_config_dir = _as_text(profile.get("remote_config_dir", ""))
        if not remote_config_dir:
            raise RuntimeError("Active SSH profile is missing remote config directory")
        files = transport.list_cfg_files(remote_config_dir)
        return {
            "ok": True,
            "profile_name": _as_text(profile.get("profile_name", "")),
            "remote_config_dir": remote_config_dir,
            "count": len(files),
            "files": files,
        }

    def _build_remote_cfg_path(self, remote_config_dir: str, username: str, file_path: str) -> str:
        """Resolve one relative cfg file path against remote config root."""
        remote_root = self._normalize_remote_root(remote_config_dir, username)
        rel_path = _as_text(file_path).replace("\\", "/").lstrip("/")
        if not rel_path:
            raise ValueError("file_path is required")
        return str(remote_root.joinpath(PurePosixPath(rel_path)))

    def sync_active_remote_cfg_to_local(
        self,
        *,
        prune_missing: bool = True,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, object]:
        """Mirror active profile remote cfg files into local config directory."""
        remote_source, profile = self._active_remote_config_source()
        runtime_config_dir = self._resolve_runtime_config_dir()
        local_source = self._runtime_local_config_source()
        remote_config_dir = _as_text(profile.get("remote_config_dir", ""))
        remote_files = remote_source.list_cfg_files()
        self._emit_operation_progress(progress_callback, "download", 0, len(remote_files) or 1)
        mirrored_rel_paths: set[str] = set()
        mirrored_checksums: dict[str, str] = {}
        synced_files = 0

        for idx, rel_posix in enumerate(remote_files, start=1):
            if not rel_posix.lower().endswith(".cfg"):
                self._emit_operation_progress(progress_callback, "download", idx, len(remote_files) or 1)
                continue

            remote_text = remote_source.read_text(rel_posix)
            local_source.write_text(rel_posix, remote_text)
            mirrored_rel_paths.add(rel_posix)
            mirrored_checksums[rel_posix] = self._text_checksum(remote_text)
            synced_files += 1
            self._emit_operation_progress(progress_callback, "download", idx, len(remote_files) or 1)

        removed_local_files = 0
        if prune_missing:
            for rel_local in local_source.list_cfg_files():
                if rel_local in mirrored_rel_paths:
                    continue
                if local_source.remove(rel_local):
                    removed_local_files += 1

        self._emit_operation_progress(progress_callback, "download", len(remote_files) or 1, len(remote_files) or 1)
        self._remote_cfg_checksums = mirrored_checksums

        return {
            "ok": True,
            "profile_name": _as_text(profile.get("profile_name", "")),
            "remote_config_dir": remote_config_dir,
            "synced_files": synced_files,
            "removed_local_files": removed_local_files,
            "local_cache_dir": str(runtime_config_dir),
        }

    def _push_local_cfg_file_to_active_remote(
        self,
        file_path: str,
        *,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, object]:
        """Upload one local cfg file back to the active remote profile path."""
        rel_path = _as_text(file_path)
        if _cfg_is_protected(rel_path):
            return {
                "uploaded": False,
                "blocked": True,
                "blocked_by_protected_file": True,
                "file_path": rel_path,
            }

        transport, profile = self._active_ssh_transport()
        remote_source, profile = self._active_remote_config_source()

        if not rel_path:
            raise ValueError("file_path is required")

        local_source = self._runtime_local_config_source()

        self._emit_operation_progress(progress_callback, "upload", 0, 1)
        payload = local_source.read_text(rel_path)
        remote_path = self._build_remote_cfg_path(
            _as_text(profile.get("remote_config_dir", "")),
            _as_text(profile.get("username", "")),
            rel_path,
        )

        expected_remote_checksum = self._remote_cfg_checksums.get(rel_path)
        if expected_remote_checksum:
            remote_current_text = remote_source.read_text(rel_path)
            remote_current_checksum = self._text_checksum(remote_current_text)
            if remote_current_checksum != expected_remote_checksum:
                raise RuntimeError(self._remote_conflict_message(rel_path, "remote file changed since last sync"))

        try:
            remote_source.write_text(rel_path, payload)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to upload cfg file '{rel_path}' to '{remote_path}': {exc}"
            ) from exc
        self._remote_cfg_checksums[rel_path] = self._text_checksum(payload)
        self._emit_operation_progress(progress_callback, "upload", 1, 1)
        return {
            "uploaded": True,
            "blocked": False,
            "blocked_by_protected_file": False,
            "file_path": rel_path,
            "remote_path": remote_path,
        }

    def _push_local_cfg_files_to_active_remote(
        self,
        file_paths: list[str],
        *,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, object]:
        """Upload a list of local cfg files and return summary metadata."""
        uploaded_paths: list[str] = []
        blocked_paths: list[str] = []
        seen: set[str] = set()
        total = len(file_paths) if file_paths else 1
        current = 0
        self._emit_operation_progress(progress_callback, "upload", 0, total)
        for rel_path in file_paths:
            normalized = _as_text(rel_path)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            if _cfg_is_protected(normalized):
                blocked_paths.append(normalized)
                current += 1
                self._emit_operation_progress(progress_callback, "upload", current, total)
                continue
            result = self._push_local_cfg_file_to_active_remote(normalized)
            remote_path = _as_text(result.get("remote_path", ""))
            if remote_path:
                uploaded_paths.append(remote_path)
            current += 1
            self._emit_operation_progress(progress_callback, "upload", current, total)

        self._emit_operation_progress(progress_callback, "upload", total, total)
        return {
            "uploaded_files": len(uploaded_paths),
            "uploaded_paths": sorted(uploaded_paths),
            "blocked_files": len(blocked_paths),
            "blocked_paths": sorted(blocked_paths),
            "blocked_by_protected_file": bool(blocked_paths),
        }

    def _sync_local_cfg_tree_to_active_remote(self, *, prune_remote_missing: bool) -> dict[str, object]:
        """Upload all local cfg files and optionally remove remote cfg files not present locally."""
        remote_source, profile = self._active_remote_config_source()

        uploaded_paths: list[str] = []
        blocked_paths: list[str] = []
        local_rel_paths: set[str] = set()
        local_source = self._runtime_local_config_source()
        for rel_path in local_source.list_cfg_files():
            local_rel_paths.add(rel_path)
            if _cfg_is_protected(rel_path):
                blocked_paths.append(rel_path)
                continue
            push_result = self._push_local_cfg_file_to_active_remote(rel_path)
            remote_path = _as_text(push_result.get("remote_path", ""))
            if remote_path:
                uploaded_paths.append(remote_path)

        removed_remote_paths: list[str] = []
        if prune_remote_missing:
            for rel_posix in remote_source.list_cfg_files():
                if rel_posix in local_rel_paths:
                    continue
                if _cfg_is_protected(rel_posix):
                    continue
                if self._remote_cfg_checksums:
                    expected_remote_checksum = self._remote_cfg_checksums.get(rel_posix)
                    if not expected_remote_checksum:
                        raise RuntimeError(
                            self._remote_conflict_message(rel_posix, "remote file appeared after last sync")
                        )
                    remote_current_text = remote_source.read_text(rel_posix)
                    remote_current_checksum = self._text_checksum(remote_current_text)
                    if remote_current_checksum != expected_remote_checksum:
                        raise RuntimeError(
                            self._remote_conflict_message(rel_posix, "remote file changed since last sync")
                        )
                if remote_source.remove(rel_posix):
                    self._remote_cfg_checksums.pop(rel_posix, None)
                    removed_remote_paths.append(
                        self._build_remote_cfg_path(
                            _as_text(profile.get("remote_config_dir", "")),
                            _as_text(profile.get("username", "")),
                            rel_posix,
                        )
                    )

        return {
            "ok": True,
            "profile_name": _as_text(profile.get("profile_name", "")),
            "uploaded_files": len(uploaded_paths),
            "removed_remote_files": len(removed_remote_paths),
            "uploaded_paths": sorted(uploaded_paths),
            "removed_remote_paths": sorted(removed_remote_paths),
            "blocked_files": len(blocked_paths),
            "blocked_paths": sorted(blocked_paths),
            "blocked_by_protected_file": bool(blocked_paths),
        }

    def _moonraker_url(self, path: str) -> str:
        """Build and validate a Moonraker URL for one API path."""
        self._refresh_moonraker_base_url_from_active_profile()
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
        """Request a Klipper host restart through Moonraker.

        Prefer the dedicated restart endpoint and fall back to RESTART gcode
        when the endpoint is unavailable on the target Moonraker instance.
        """
        try:
            result = self._moonraker_post_command(
                path="/printer/restart",
                timeout=timeout,
                json_body=None,
                error_prefix="Moonraker restart request",
            )
            payload = result.as_dict()
            payload["restart_method"] = "endpoint"
            return payload
        except RuntimeError as primary_error:
            try:
                fallback = self._moonraker_post_command(
                    path="/printer/gcode/script",
                    timeout=timeout,
                    json_body={"script": "RESTART"},
                    error_prefix="Moonraker restart fallback request",
                )
                payload = fallback.as_dict()
                payload["restart_method"] = "gcode_script"
                payload["restart_fallback_from_error"] = str(primary_error)
                return payload
            except RuntimeError:
                raise primary_error

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

    def index(
        self,
        progress_callback: Callable[[str, int, int], None] | None = None,
        *,
        sync_remote: bool = True,
    ) -> dict[str, object]:
        """Run config indexing with configured retention settings."""
        sync_result: dict[str, object] | None = None
        runtime_config_dir = self._resolve_runtime_config_dir()
        if self._runtime_mode == "off_printer" and sync_remote:
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
            progress_callback=_parse_progress,
        )
        if sync_result is not None:
            result["remote_sync"] = sync_result
        result["runtime_config_dir"] = str(runtime_config_dir)
        return result

    def load_cfg_loading_overview(self) -> dict[str, object]:
        """Load cfg parse-order overview for Klipper and KlipperVault."""
        if self._runtime_mode == "off_printer":
            remote_source, _ = self._active_remote_config_source()
            return get_cfg_loading_overview_from_source(remote_source)
        return get_cfg_loading_overview(self._resolve_runtime_config_dir())

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

    def create_backup(self, name: str) -> dict[str, object]:
        """Create a named backup snapshot from current macro state."""
        return create_macro_backup(
            db_path=self._db_path,
            backup_name=name,
            config_dir=self._resolve_runtime_config_dir(),
            config_source=self._runtime_local_config_source(),
            printer_profile_id=self._active_printer_profile_id,
        )

    def list_backups(self) -> list[dict[str, object]]:
        """Return all available backups."""
        return list_macro_backups(self._db_path, printer_profile_id=self._active_printer_profile_id)

    def load_backup_contents(self, backup_id: int) -> list[dict[str, object]]:
        """Return snapshot items for one backup."""
        return load_backup_items(self._db_path, backup_id, printer_profile_id=self._active_printer_profile_id)

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

    def save_config_to_remote(
        self,
        *,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, object]:
        """Explicitly sync local cfg tree to remote printer config via SFTP."""
        if self._runtime_mode != "off_printer":
            return {
                "ok": False,
                "uploaded_files": 0,
                "removed_remote_files": 0,
                "uploaded_paths": [],
                "removed_remote_paths": [],
                "blocked_files": 0,
                "blocked_paths": [],
                "blocked_by_protected_file": False,
            }

        self._emit_operation_progress(progress_callback, "upload", 0, 1)
        result = self._sync_local_cfg_tree_to_active_remote(prune_remote_missing=False)
        self._emit_operation_progress(progress_callback, "upload", 1, 1)
        result["remote_synced"] = bool(_as_int(result.get("uploaded_files", 0), default=0) > 0)
        self._append_restart_policy_result(result, uploaded_files=_as_int(result.get("uploaded_files", 0), default=0))
        return result

    def delete_backup(self, backup_id: int) -> dict[str, object]:
        """Delete one backup snapshot."""
        return delete_macro_backup(
            db_path=self._db_path,
            backup_id=backup_id,
            printer_profile_id=self._active_printer_profile_id,
        )

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
        except ValidationError:
            return None

    def export_macro_share_file(
        self,
        identities: list[tuple[str, str]],
        source_vendor: str,
        source_model: str,
        out_file: Path,
    ) -> dict[str, object]:
        """Export selected latest macros to a shareable JSON file."""
        payload = self.export_macro_share_payload_data(
            identities=identities,
            source_vendor=source_vendor,
            source_model=source_model,
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

        result = self.import_macro_share_payload_data(
            payload=payload,
            target_vendor=target_vendor,
            target_model=target_model,
        )

        return {
            **result,
            "file_path": str(import_file),
        }

    def export_macro_share_payload_data(
        self,
        *,
        identities: list[tuple[str, str]],
        source_vendor: str,
        source_model: str,
    ) -> dict[str, object]:
        """Return share-file payload without writing to disk."""
        return export_macro_share_payload(
            db_path=self._db_path,
            identities=identities,
            source_vendor=source_vendor,
            source_model=source_model,
            now_ts=int(time.time()),
        )

    def import_macro_share_payload_data(
        self,
        *,
        payload: dict[str, object],
        target_vendor: str,
        target_model: str,
    ) -> dict[str, object]:
        """Import macros from pre-loaded share payload data."""

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
            printer_profile_id=(int(self._active_printer_profile_id) if int(self._active_printer_profile_id) > 0 else None),
            now_ts=int(time.time()),
        )

        imported_items = _as_list(import_result.get("imported_items", []))
        activate_set = {_as_text(identity) for identity in activate_identities}
        activated_count = 0
        activated_files: list[str] = []
        runtime_config_dir = self._resolve_runtime_config_dir()

        for item in imported_items:
            parsed_item = self._parse_imported_update_item(item)
            if parsed_item is None or parsed_item.identity not in activate_set:
                continue

            restore_macro_version(
                db_path=self._db_path,
                config_dir=runtime_config_dir,
                file_path=parsed_item.file_path,
                macro_name=parsed_item.macro_name,
                version=parsed_item.version,
            )
            activated_count += 1
            activated_files.append(parsed_item.file_path)

        imported_count = _as_int(import_result.get("imported", 0))
        result = {
            "imported": imported_count,
            "activated": activated_count,
            "imported_items": imported_items,
        }

        if activated_files:
            result["remote_synced"] = False
            result["local_changed"] = True

        return result

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
