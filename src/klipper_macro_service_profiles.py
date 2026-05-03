#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Printer profile, SSH, remote-sync, and Moonraker mixin for MacroGuiService.

Extracted from klipper_macro_gui_service to keep concern-specific logic
in focused, navigable modules. MacroGuiService inherits this mixin.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, field_validator
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

if TYPE_CHECKING:
    from klipper_vault_secret_store import CredentialStore

from klipper_vault_config_source import LocalConfigSource, SshConfigSource
from klipper_vault_db import open_sqlite_connection
from klipper_vault_remote_profiles import (
    delete_ssh_host_profile,
    SshHostProfile,
    get_active_ssh_host_profile,
    get_credential_backend,
    list_ssh_host_profiles,
    set_active_ssh_host_profile,
    upsert_ssh_host_profile,
)
from klipper_vault_ssh_transport import SshConnectionConfig, SshTransport
from klipper_vault_printer_profiles import (
    create_printer_profile,
    ensure_printer_profile_schema,
    get_active_printer_profile,
    get_printer_profile_by_ssh_profile_id,
    list_printer_profiles,
    set_active_printer_profile,
    update_printer_profile_connection,
    update_printer_profile_identity,
)
from klipper_type_utils import to_int as _as_int
from klipper_type_utils import to_text as _as_text
from klipper_type_utils import cfg_is_protected as _cfg_is_protected


_LOG = logging.getLogger(__name__)


_FREEDI_CFG_FILENAME = "freedi.cfg"


def _read_key_value_line(raw_line: str) -> tuple[str, str] | None:
    """Parse simple cfg lines like `key: value` or `key = value`."""
    line = raw_line.split("#", 1)[0].strip()
    if not line:
        return None
    for separator in (":", "="):
        if separator in line:
            key, value = line.split(separator, 1)
            key = key.strip().lower()
            value = value.strip()
            if key:
                return key, value
    return None


def _detect_freedi_identity_from_cfg(freedi_cfg_content: str) -> tuple[str, str]:
    """Detect FreeDi printer model from freedi.cfg content.

    Returns ("FreeDi", printer_model) when a printer_model key is present,
    or ("FreeDi", "") when the file exists but lacks a printer_model value.
    """
    for raw_line in freedi_cfg_content.splitlines():
        parsed = _read_key_value_line(raw_line)
        if parsed is None:
            continue
        key, value = parsed
        if key == "printer_model" and value:
            return "FreeDi", value
    return "FreeDi", ""


def _detect_printer_identity_from_cfg(printer_cfg_content: str) -> tuple[str, str]:
    """Detect printer vendor and model from printer.cfg content by pattern matching.

    Returns a tuple of (vendor, model). Returns ("", "") if detection fails.
    """
    if not printer_cfg_content:
        return "", ""
    
    content_lower = printer_cfg_content.lower()
    
    # VORON detection - multiple variants
    if "[stepper_a]" in content_lower or "[stepper_b]" in content_lower or "[stepper_c]" in content_lower:
        if "[stepper_d]" in content_lower:
            return "Voron", "V2.4"  # V2.4 uses 4 steppers
        elif "[display_template" in content_lower or "[voron_display]" in content_lower:
            # Check for V2.4 indicators
            if any(x in content_lower for x in ["[led_effects", "[case_light"]):
                return "Voron", "V2.4"
            return "Voron", "V2.4"
        return "Voron", "Trident"  # 3-stepper systems
    
    # VORON V0 detection
    if "[stepper_x]" in content_lower and "[stepper_y]" in content_lower:
        # V0 and V2.4 both use X/Y, check for V0-specific patterns
        if "[extruder_stepper" not in content_lower and "[extruder1" not in content_lower:
            # V0 detection through absence of multi-tool patterns and presence of CoreXY-compatible stepper names
            if "[temperature_fan" in content_lower:
                return "Voron", "V0"
    
    # VORON Switchwire detection  
    if "[stepper_z]" in content_lower and "[stepper_z1]" in content_lower:
        if "[dual_carriage" not in content_lower:  # Not a V2.4
            return "Voron", "Switchwire"
    
    # Rat Rig V-Core detection
    if "[led_light" in content_lower or "[temperature_fan chamber" in content_lower:
        if "[nozzle_scrub" in content_lower:
            if "[bed_mesh" in content_lower:
                return "RatRig", "V-Core 3"
            return "RatRig", "V-Core"
    
    # Artillery detection - look for common Artillery config patterns
    if "[artillery_z_motor_helper" in content_lower or "[z_calibration" in content_lower:
        if "artillery" in content_lower or "[nozzle_scrub" in content_lower:
            return "Artillery", "Sidewinder"
    
    # Prusa detection
    if "[mmu" in content_lower or "[mmu2" in content_lower:
        if "[extruder" in content_lower and "[extruder1" in content_lower:
            return "Prusa", "MK3.9"
        return "Prusa", "MK3"
    
    # Creality/Ender detection
    if "[ender" in content_lower or "[creality" in content_lower:
        if "ender5" in content_lower:
            return "Creality", "Ender 5"
        elif "ender3" in content_lower:
            return "Creality", "Ender 3"
        return "Creality", "Ender"
    
    # Generic CoreXY detection
    if "[stepper_x]" in content_lower and "[stepper_y]" in content_lower:
        if "[stepper_z]" in content_lower and "[stepper_z1]" in content_lower:
            return "Generic", "CoreXY"
    
    return "", ""


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


def _normalize_standard_moonraker_url(moonraker_url: str, ssh_host: str) -> str:
    """Rewrite localhost Moonraker URLs to the remote SSH host for standard mode."""
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


def _normalize_remote_root(remote_config_dir: str, username: str) -> PurePosixPath:
    """Normalize remote config root and expand leading ~/ using profile username."""
    raw_root = _as_text(remote_config_dir).strip()
    user = _as_text(username).strip()
    if raw_root == "~":
        raw_root = f"/home/{user}" if user else "/"
    elif raw_root.startswith("~/"):
        raw_root = f"/home/{user}/{raw_root[2:]}" if user else f"/{raw_root[2:]}"
    return PurePosixPath(raw_root or "/")


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


def _normalize_credential_ref(profile_name: str, auth_mode: str) -> str:
    """Build a stable credential reference for profile secrets."""
    cleaned_name = "-".join(_as_text(profile_name).lower().split()) or "default"
    cleaned_auth_mode = _as_text(auth_mode).lower() or "key"
    return f"ssh:{cleaned_name}:{cleaned_auth_mode}"


def _moonraker_url_from_base(base_url: str, path: str) -> str:
    """Build and validate a Moonraker URL for one API path from a given base URL."""
    parsed = urlparse(_as_text(base_url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Moonraker URL must use http/https.")
    clean_path = path if path.startswith("/") else f"/{path}"
    return f"{parsed.geturl().rstrip('/')}{clean_path}"


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


def _decode_json_payload(response: httpx.Response) -> dict[str, object]:
    """Decode JSON response payload with a safe fallback to empty dict."""
    if not response.text:
        return {}
    try:
        decoded = response.json()
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


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


class PrinterProfileMixin:
    """Printer profile, SSH transport, remote cfg sync, and Moonraker operations."""

    # ------------------------------------------------------------------
    # Attributes provided by MacroGuiService at runtime
    # ------------------------------------------------------------------
    _db_path: Path
    _runtime_mode: str
    _credential_store: "CredentialStore"

    def cleanup_runtime_cache(self) -> None:
        raise NotImplementedError  # provided by MacroGuiService

    def _resolve_runtime_config_dir(self) -> Path:
        raise NotImplementedError  # provided by MacroGuiService

    def _append_restart_policy_result(self, result: dict[str, object], *, uploaded_files: int) -> None:
        raise NotImplementedError  # provided by MacroGuiService

    @staticmethod
    def _require_non_empty(value: str, error_message: str) -> str:
        raise NotImplementedError  # provided by MacroGuiService

    @staticmethod
    def _emit_operation_progress(
        callback: Callable[[str, int, int], None] | None,
        phase: str,
        current: int,
        total: int,
    ) -> None:
        raise NotImplementedError  # provided by MacroGuiService

    @staticmethod
    def _text_checksum(text: str) -> str:
        raise NotImplementedError  # provided by MacroGuiService

    @staticmethod
    def _remote_conflict_message(rel_path: str, reason: str) -> str:
        raise NotImplementedError  # provided by MacroGuiService

    # ------------------------------------------------------------------ #
    # Printer profiles                                                     #
    # ------------------------------------------------------------------ #

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
                    "is_virtual": profile.is_virtual,
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
            "is_virtual": profile.is_virtual,
        }

    def create_virtual_printer_profile(
        self,
        *,
        profile_name: str,
        vendor: str,
        model: str,
        activate: bool = True,
    ) -> dict[str, object]:
        """Create a developer virtual printer profile for local-only workflows."""
        normalized_name = self._require_non_empty(profile_name, "profile_name is required")
        normalized_vendor = self._require_non_empty(vendor, "vendor is required")
        normalized_model = self._require_non_empty(model, "model is required")

        created_profile_id = create_printer_profile(
            self._db_path,
            profile_name=normalized_name,
            vendor=normalized_vendor,
            model=normalized_model,
            connection_type="standard",
            ssh_profile_id=None,
            is_active=bool(activate),
            is_virtual=True,
        )

        if activate:
            self.activate_printer_profile(created_profile_id)

        return {
            "ok": True,
            "profile_id": int(created_profile_id),
            "created": True,
            "is_virtual": True,
            "is_active": bool(activate),
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
        
        # Try to auto-detect printer vendor/model if not yet set
        if profile is None or not bool(profile.is_virtual):
            self._try_detect_printer_identity()
        
        return {"ok": True, "profile_id": int(profile_id)}

    def delete_printer_profile(self, profile_id: int) -> dict[str, object]:
        """Delete a printer profile and all associated data."""
        try:
            from klipper_vault_printer_profiles import delete_printer_profile as delete_profile_impl
            success = delete_profile_impl(self._db_path, int(profile_id))
            return {
                "ok": success,
                "error": "" if success else "Failed to delete printer profile"
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc)
            }

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
            connection_type="standard",
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

    def _try_detect_printer_identity(self) -> None:
        """Attempt to auto-detect printer vendor/model via SSH if not yet set.

        Checks for freedi.cfg first (FreeDi printers), then falls back to
        pattern matching against printer.cfg content.
        """
        profile = get_active_printer_profile(self._db_path)
        if profile is None or profile.id is None:
            return

        # Only detect if both vendor and model are empty
        vendor = _as_text(profile.vendor).strip()
        model = _as_text(profile.model).strip()
        if vendor or model:
            return  # Already has identity set

        try:
            source, _ = self._active_remote_config_source()

            # FreeDi detection: presence of freedi.cfg is the primary signal.
            try:
                freedi_content = source.read_text(_FREEDI_CFG_FILENAME)
                detected_vendor, detected_model = _detect_freedi_identity_from_cfg(freedi_content)
            except Exception:
                # freedi.cfg not present — fall back to printer.cfg pattern matching
                detected_vendor, detected_model = "", ""
                try:
                    printer_cfg_content = source.read_text("printer.cfg")
                    detected_vendor, detected_model = _detect_printer_identity_from_cfg(printer_cfg_content)
                except Exception:
                    _LOG.debug("Printer identity detection fallback via printer.cfg failed", exc_info=True)

            if detected_vendor or detected_model:
                update_printer_profile_identity(
                    self._db_path,
                    profile_id=int(profile.id),
                    vendor=detected_vendor,
                    model=detected_model,
                )
        except Exception:
            # Detection is optional, so keep activation non-blocking while preserving diagnostics.
            _LOG.debug("Printer identity auto-detection failed", exc_info=True)

    def _refresh_moonraker_base_url_from_active_profile(self) -> None:
        """Refresh Moonraker base URL from active standard profile when available."""
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
            normalized = _normalize_standard_moonraker_url(profile_moonraker_url, profile_ssh_host)
            self._moonraker_base_url = normalized.rstrip("/")

    # ------------------------------------------------------------------ #
    # SSH profiles                                                         #
    # ------------------------------------------------------------------ #

    def list_ssh_profiles(self) -> list[dict[str, object]]:
        """Return standard connection profiles (owned by printer profiles)."""
        payloads: list[dict[str, object]] = []
        for printer_profile in self.list_printer_profiles():
            if bool(printer_profile.get("is_virtual", False)):
                continue
            if not _as_text(printer_profile.get("ssh_host", "")):
                continue
            payload = _printer_profile_to_ssh_payload(printer_profile)
            credential_ref = _as_text(payload.get("credential_ref", ""))
            backend = get_credential_backend(self._db_path, credential_ref) if credential_ref else None
            payload["secret_backend"] = backend or ""
            payload["has_secret"] = bool(self._credential_store.get_secret(credential_ref=credential_ref))
            payloads.append(payload)
        return payloads

    def get_active_ssh_profile(self) -> dict[str, object] | None:
        """Return active standard connection settings from active printer profile."""
        active_printer = self.get_active_printer_profile()
        if isinstance(active_printer, dict) and active_printer:
            payload = _printer_profile_to_ssh_payload(active_printer)
            payload["is_virtual"] = bool(active_printer.get("is_virtual", False))
            if _as_text(payload.get("host", "")):
                credential_ref = _as_text(payload.get("credential_ref", ""))
                backend = get_credential_backend(self._db_path, credential_ref) if credential_ref else None
                payload["secret_backend"] = backend or ""
                payload["has_secret"] = bool(self._credential_store.get_secret(credential_ref=credential_ref))
                return payload
            if bool(active_printer.get("is_virtual", False)):
                payload["has_secret"] = True
                return payload

        # Legacy fallback while older profile rows are still in use.
        profile = get_active_ssh_host_profile(self._db_path)
        if profile is None:
            return None
        payload = _profile_to_dict(profile)
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
        moonraker_url = _normalize_standard_moonraker_url(moonraker_url, host)
        auth_mode = (_as_text(auth_mode).lower() or "key")
        if auth_mode not in {"key", "password"}:
            raise ValueError("auth_mode must be 'key' or 'password'")

        normalized_credential_ref = _as_text(credential_ref) or _normalize_credential_ref(profile_name, auth_mode)
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
        """Activate one saved standard connection profile."""
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

    # ------------------------------------------------------------------ #
    # SSH transport / remote config sources                               #
    # ------------------------------------------------------------------ #

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
        """Validate active standard SSH profile connectivity."""
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
        remote_root = _normalize_remote_root(remote_config_dir, username)
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
        allow_protected_file: bool = False,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, object]:
        """Upload one local cfg file back to the active remote profile path."""
        rel_path = _as_text(file_path)
        if _cfg_is_protected(rel_path) and not allow_protected_file:
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

    def _sync_local_cfg_tree_to_active_remote(
        self,
        *,
        prune_remote_missing: bool,
        allow_protected_upload: bool = False,
    ) -> dict[str, object]:
        """Upload all local cfg files and optionally remove remote cfg files not present locally."""
        remote_source, profile = self._active_remote_config_source()

        uploaded_paths: list[str] = []
        blocked_paths: list[str] = []
        local_rel_paths: set[str] = set()
        local_source = self._runtime_local_config_source()
        for rel_path in local_source.list_cfg_files():
            local_rel_paths.add(rel_path)
            if _cfg_is_protected(rel_path) and not allow_protected_upload:
                blocked_paths.append(rel_path)
                continue
            push_result = self._push_local_cfg_file_to_active_remote(
                rel_path,
                allow_protected_file=allow_protected_upload,
            )
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

    def save_config_to_remote(
        self,
        *,
        allow_protected_upload: bool = False,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, object]:
        """Explicitly sync local cfg tree to remote printer config via SFTP."""
        active_printer = self.get_active_printer_profile()
        if isinstance(active_printer, dict) and bool(active_printer.get("is_virtual", False)):
            return {
                "ok": True,
                "uploaded_files": 0,
                "removed_remote_files": 0,
                "uploaded_paths": [],
                "removed_remote_paths": [],
                "blocked_files": 0,
                "blocked_paths": [],
                "blocked_by_protected_file": False,
                "remote_synced": False,
                "restart_required": False,
                "dynamic_reload_required": False,
                "restart_message": "Virtual printer profile: remote upload is skipped.",
            }

        if self._runtime_mode != "standard":
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
        # Save Config mirrors the local cfg tree to remote, including removals.
        result = self._sync_local_cfg_tree_to_active_remote(
            prune_remote_missing=True,
            allow_protected_upload=allow_protected_upload,
        )
        self._emit_operation_progress(progress_callback, "upload", 1, 1)
        result["remote_synced"] = bool(_as_int(result.get("uploaded_files", 0), default=0) > 0)
        self._append_restart_policy_result(result, uploaded_files=_as_int(result.get("uploaded_files", 0), default=0))
        return result

    # ------------------------------------------------------------------ #
    # Moonraker HTTP                                                       #
    # ------------------------------------------------------------------ #

    def _moonraker_url(self, path: str) -> str:
        """Build and validate a Moonraker URL for one API path."""
        self._refresh_moonraker_base_url_from_active_profile()
        parsed = urlparse(self._moonraker_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Moonraker URL must use http/https.")
        clean_path = path if path.startswith("/") else f"/{path}"
        return f"{self._moonraker_base_url}{clean_path}"

    def _moonraker_base_url_for_profile(self, profile_id: int) -> str:
        """Resolve Moonraker base URL for a specific printer profile id."""
        target_profile_id = int(profile_id)
        target_profile: dict[str, object] | None = None
        for profile in self.list_printer_profiles():
            if _as_int(profile.get("id"), default=0) == target_profile_id:
                target_profile = profile
                break

        if not isinstance(target_profile, dict):
            raise ValueError("Printer profile not found")

        moonraker_url = _as_text(target_profile.get("ssh_moonraker_url", "")).strip()
        ssh_host = _as_text(target_profile.get("ssh_host", "")).strip()

        if not moonraker_url:
            ssh_profile_id = _as_int(target_profile.get("ssh_profile_id"), default=0)
            if ssh_profile_id > 0:
                ssh_profile = self._find_ssh_profile(ssh_profile_id)
                if ssh_profile is not None:
                    moonraker_url = _as_text(ssh_profile.moonraker_url).strip()
                    if not ssh_host:
                        ssh_host = _as_text(ssh_profile.host).strip()

        if not moonraker_url:
            raise ValueError("Printer profile is missing Moonraker URL")

        return _normalize_standard_moonraker_url(moonraker_url, ssh_host).rstrip("/")

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
            response = _moonraker_post(url, json_body=json_body, timeout=timeout)
        except httpx.HTTPError as exc:
            raise RuntimeError(str(exc)) from exc

        payload = _decode_json_payload(response)
        if response.status_code >= 400:
            error_message = _error_message_from_response(response, payload)
            raise RuntimeError(error_message or f"{error_prefix} failed with status {response.status_code}")

        return MoonrakerCommandResult(
            ok=True,
            status=response.status_code,
            payload=payload,
        )

    def query_printer_status(self, timeout: float = 2.0) -> dict[str, object]:
        """Query Moonraker print stats and return normalized printer status."""
        active_profile = self.get_active_printer_profile()
        if isinstance(active_profile, dict) and bool(active_profile.get("is_virtual", False)):
            return MoonrakerStatusResult(
                connected=True,
                state="virtual",
                message="Virtual printer (local-only mode)",
                is_printing=False,
                is_busy=False,
            ).as_dict()

        try:
            url = self._moonraker_url("/printer/objects/query")
            response = _moonraker_get(url, params={"print_stats": "state,message"}, timeout=timeout)
            payload = _decode_json_payload(response)
        except (ValueError, httpx.HTTPError) as exc:
            return MoonrakerStatusResult(
                connected=False,
                state="unknown",
                message=str(exc),
                is_printing=False,
                is_busy=False,
            ).as_dict()

        if response.status_code >= 400:
            error_message = _error_message_from_response(response, payload)
            return MoonrakerStatusResult(
                connected=False,
                state="unknown",
                message=error_message or f"Moonraker status request failed with status {response.status_code}",
                is_printing=False,
                is_busy=False,
            ).as_dict()

        return _status_result_from_payload(payload).as_dict()

    def query_printer_status_for_profile(self, profile_id: int, timeout: float = 2.0) -> dict[str, object]:
        """Query Moonraker status for one printer profile without changing active state."""
        profile_payload = next(
            (
                profile
                for profile in self.list_printer_profiles()
                if _as_int(profile.get("id"), default=0) == int(profile_id)
            ),
            None,
        )
        if isinstance(profile_payload, dict) and bool(profile_payload.get("is_virtual", False)):
            result = MoonrakerStatusResult(
                connected=True,
                state="virtual",
                message="Virtual printer (local-only mode)",
                is_printing=False,
                is_busy=False,
            ).as_dict()
            result["profile_id"] = int(profile_id)
            return result

        try:
            base_url = self._moonraker_base_url_for_profile(profile_id)
            url = _moonraker_url_from_base(base_url, "/printer/objects/query")
            response = _moonraker_get(url, params={"print_stats": "state,message"}, timeout=timeout)
            payload = _decode_json_payload(response)
        except (ValueError, httpx.HTTPError) as exc:
            result = MoonrakerStatusResult(
                connected=False,
                state="unknown",
                message=str(exc),
                is_printing=False,
                is_busy=False,
            ).as_dict()
            result["profile_id"] = int(profile_id)
            return result

        if response.status_code >= 400:
            error_message = _error_message_from_response(response, payload)
            result = MoonrakerStatusResult(
                connected=False,
                state="unknown",
                message=error_message or f"Moonraker status request failed with status {response.status_code}",
                is_printing=False,
                is_busy=False,
            ).as_dict()
            result["profile_id"] = int(profile_id)
            return result

        result = _status_result_from_payload(payload).as_dict()
        result["profile_id"] = int(profile_id)
        return result

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
