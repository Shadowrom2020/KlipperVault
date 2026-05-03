#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure helper functions and constants for the KlipperVault NiceGUI frontend."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from nicegui import ui

from klipper_type_utils import to_int as _to_int
from klipper_vault_i18n import t

_STATUS_BADGE_CLASSES: dict[str, str] = {
    "deleted": "text-[10px] uppercase tracking-wide text-white bg-grey-6 rounded px-1.5 py-0.5",
    "new": "text-[10px] uppercase tracking-wide text-white bg-purple-7 rounded px-1.5 py-0.5",
    "not_loaded": "text-[10px] uppercase tracking-wide text-white bg-orange-7 rounded px-1.5 py-0.5",
    "dynamic": "text-[10px] uppercase tracking-wide text-white bg-blue-7 rounded px-1.5 py-0.5",
    "renamed": "text-[10px] uppercase tracking-wide text-white bg-blue-8 rounded px-1.5 py-0.5",
    "active": "text-[10px] uppercase tracking-wide text-white bg-green-8 rounded px-1.5 py-0.5",
    "inactive": "text-[10px] uppercase tracking-wide text-black bg-yellow-6 rounded px-1.5 py-0.5",
}


def to_optional_int(value: object) -> int | None:
    """Convert dynamic payload value to int or None when unavailable."""
    if value is None:
        return None
    return _to_int(value)


def file_operation_phase_text(phase: str) -> str:
    """Map backend phase keys to user-facing file-operation text."""
    normalized = str(phase or "").strip().lower()
    if normalized == "download":
        return t("Downloading cfg files from printer...")
    if normalized == "upload":
        return t("Uploading changed cfg files to printer...")
    if normalized == "parse":
        return t("Parsing local cfg files...")
    return t("Working on files...")


def translated_active_filter_state(active_filter: str) -> str:
    """Return localized active-filter state label for button text."""
    normalized = str(active_filter or "").strip().lower()
    if normalized == "active":
        return t("active")
    if normalized == "inactive":
        return t("inactive")
    return t("all")


def status_badge_key(macro: dict[str, object]) -> str:
    """Resolve macro row status key for consistent badge rendering."""
    if macro.get("is_deleted", False):
        return "deleted"
    if macro.get("is_new", False):
        return "new"
    if not macro.get("is_loaded", True):
        return "not_loaded"
    if macro.get("is_active", False) and macro.get("is_dynamic", False):
        return "dynamic"
    if macro.get("is_active", False) and macro.get("renamed_from"):
        return "renamed"
    if macro.get("is_active", False):
        return "active"
    return "inactive"


def default_keep_file(entries: list[dict[str, object]]) -> str:
    """Choose default keep target, preferring currently active entry."""
    for entry in entries:
        if entry.get("is_active", False):
            return str(entry.get("file_path", ""))
    return str(entries[0].get("file_path", "")) if entries else ""


def format_moonraker_url_host(host: str) -> str:
    """Return a URL-safe host string for the Moonraker endpoint."""
    normalized_host = str(host or "").strip() or "127.0.0.1"
    if ":" in normalized_host and not normalized_host.startswith("["):
        return f"[{normalized_host}]"
    return normalized_host


def is_remote_conflict_error(error: Exception | str) -> bool:
    """Return True when an error indicates stale remote cfg state."""
    text = str(error or "").lower()
    return "remote cfg conflict" in text


def is_dynamic_version_row(version_row: dict[str, object]) -> bool:
    """Return True when selected macro version is sourced from dynamic configs."""
    return bool(version_row.get("is_dynamic", False))


def default_pr_head_branch(source_vendor: str, source_model: str) -> str:
    """Build a unique default branch name for PR publishing."""
    vendor = str(source_vendor or "").lower().replace(" ", "-") or "printer"
    model = str(source_model or "").lower().replace(" ", "-") or "model"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"klippervault/{vendor}-{model}/{stamp}"


def dynamic_macro_file_paths(macros: list[dict[str, object]]) -> set[str]:
    """Collect dynamic macro file paths from cached macro rows."""
    return {
        str(macro.get("file_path", ""))
        for macro in macros
        if bool(macro.get("is_dynamic", False))
    }


def normalize_touched_cfg_paths(paths: list[str], config_dir: Path) -> set[str]:
    """Normalize touched cfg paths to relative and basename match keys."""
    normalized: set[str] = set()
    for raw in paths:
        path_str = str(raw or "").strip()
        if not path_str:
            continue
        candidate = Path(path_str)
        if candidate.is_absolute():
            try:
                path_str = str(candidate.resolve().relative_to(config_dir.resolve()))
            except Exception:
                path_str = str(candidate)
        normalized.add(path_str)
        normalized.add(str(Path(path_str).name))
    return normalized


def paths_include_dynamic_macros(normalized_paths: set[str], dynamic_files: set[str]) -> bool:
    """Return True when normalized touched paths include any dynamic macro file."""
    for dynamic_file in dynamic_files:
        if dynamic_file in normalized_paths:
            return True
        if Path(dynamic_file).name in normalized_paths:
            return True
    return False


def standard_profile_status(ready: bool, detail: str) -> tuple[str, str]:
    """Return status text and label class for standard profile readiness."""
    detail_text = str(detail or "").strip()
    if ready:
        status_text = t("Printer connection ready")
        if detail_text:
            status_text = t("Printer connection ready: {detail}", detail=detail_text)
        return status_text, "text-xs text-positive"

    status_text = t("No active printer connection configured")
    if detail_text:
        status_text = t("No active printer connection configured: {detail}", detail=detail_text)
    return status_text, "text-xs text-negative"


def printer_offline_status_text(detail: str) -> str:
    """Return printer-offline status text with optional detail."""
    detail_text = str(detail or "").strip()
    if detail_text:
        return t("Printer offline: {detail}", detail=detail_text)
    return t("Printer offline")


def progress_value_and_percent(current: int, total: int) -> tuple[float, int]:
    """Normalize progress inputs into clamped value and rounded percent."""
    display_total = max(int(total), 1)
    progress_value = min(max(int(current) / display_total, 0.0), 1.0)
    percent = int(round(progress_value * 100.0))
    return progress_value, percent


def reload_button_state(
    *,
    printer_is_printing: bool,
    printer_is_busy: bool,
    restart_required: bool,
    dynamic_reload_required: bool,
) -> tuple[bool, bool]:
    """Return visibility/enabled flags for restart and dynamic reload actions."""
    is_allowed = (not printer_is_printing) and (not printer_is_busy)
    show_restart = restart_required and is_allowed
    # Dynamic macros can be reloaded while printing.
    show_dynamic_reload = (not restart_required) and dynamic_reload_required
    return show_restart, show_dynamic_reload


def save_config_button_enabled(
    *,
    is_ready: bool,
    printer_is_printing: bool,
    has_unsynced_local_changes: bool,
    is_virtual_printer: bool,
) -> bool:
    """Return True when Save Config action should be enabled."""
    if is_virtual_printer:
        return False
    can_upload_now = is_ready and (not printer_is_printing)
    return can_upload_now and has_unsynced_local_changes


def normalized_theme_mode(value: object) -> str:
    """Normalize a persisted theme mode value to one supported option."""
    mode = str(value or "auto").strip().lower()
    if mode in {"auto", "light", "dark"}:
        return mode
    return "auto"


def apply_theme_mode(dark_mode: ui.dark_mode, theme_mode: str) -> None:
    """Apply one theme mode to the NiceGUI dark mode controller."""
    norm = normalized_theme_mode(theme_mode)
    if norm == "light":
        dark_mode.disable()
        return
    if norm == "dark":
        dark_mode.enable()
        return
    dark_mode.auto()



def safe_notify(
    message: str,
    notify_type: Literal["positive", "negative", "warning", "info", "ongoing"] = "info",
) -> None:
    """Best-effort notify helper for callbacks that may outlive their UI slot."""
    try:
        ui.notify(message, type=notify_type)
    except RuntimeError:
        # NiceGUI can lose slot context when callbacks rerender/delete parent elements.
        return
