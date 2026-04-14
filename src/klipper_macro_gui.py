#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
# mypy: ignore-errors
"""NiceGUI frontend for Klipper macro indexing."""

from __future__ import annotations

import asyncio
from datetime import datetime
import os

from pathlib import Path
import tempfile
import time
from urllib.parse import urlparse, urlunparse

from nicegui import ui

from klipper_macro_compare import MacroCompareView
from klipper_macro_gui_logic import (
    duplicate_names_for_macros,
    filter_macros,
    find_active_override,
    macro_key,
    selected_or_first_macro,
    sort_macros,
)
from klipper_macro_gui_service import MacroGuiService
from klipper_macro_gui_state import UIState
from klipper_macro_viewer import MacroViewer, format_ts as _format_ts
from klipper_type_utils import to_dict_list as _as_dict_list
from klipper_type_utils import to_int as _to_int
from klipper_vault_config import VaultConfig, load_or_create as _load_vault_config, save as _save_vault_config
from klipper_vault_paths import DEFAULT_CONFIG_DIR, DEFAULT_DB_PATH
from klipper_vault_i18n import set_language, t

_STATUS_BADGE_CLASSES: dict[str, str] = {
    "deleted": "text-[10px] uppercase tracking-wide text-white bg-grey-6 rounded px-1.5 py-0.5",
    "new": "text-[10px] uppercase tracking-wide text-white bg-purple-7 rounded px-1.5 py-0.5",
    "not_loaded": "text-[10px] uppercase tracking-wide text-white bg-orange-7 rounded px-1.5 py-0.5",
    "dynamic": "text-[10px] uppercase tracking-wide text-white bg-blue-7 rounded px-1.5 py-0.5",
    "renamed": "text-[10px] uppercase tracking-wide text-white bg-blue-8 rounded px-1.5 py-0.5",
    "active": "text-[10px] uppercase tracking-wide text-white bg-green-8 rounded px-1.5 py-0.5",
    "inactive": "text-[10px] uppercase tracking-wide text-black bg-yellow-6 rounded px-1.5 py-0.5",
}


def _to_optional_int(value: object) -> int | None:
    """Convert dynamic payload value to int or None when unavailable."""
    if value is None:
        return None
    return _to_int(value)


def build_ui(app_version: str = "unknown") -> None:
    """Build the full NiceGUI interface and wire all callbacks."""
    config_dir = Path(DEFAULT_CONFIG_DIR).expanduser().resolve()
    db_path = Path(DEFAULT_DB_PATH).expanduser().resolve()
    # Load (or create) app settings from SQLite once at startup.
    # All subsequent indexing runs read settings from this in-memory object.
    vault_cfg = _load_vault_config(config_dir, db_path)
    set_language(os.environ.get("KLIPPERVAULT_LANG", vault_cfg.ui_language))
    ui.page_title(t("Klipper Vault"))
    runtime_mode = "off_printer"
    off_printer_mode_enabled = True
    service = MacroGuiService(
        db_path=db_path,
        config_dir=config_dir,
        version_history_size=vault_cfg.version_history_size,
        runtime_mode=runtime_mode,
        moonraker_base_url=os.environ.get("MOONRAKER_BASE_URL", ""),
    )

    # ── Initialize UIState container ─────────────────────────────────────────
    state = UIState(
        service=service,
        config_dir=config_dir,
        app_version=app_version,
        off_printer_profile_ready=not off_printer_mode_enabled,
        list_page_size=max(50, _to_int(os.environ.get("KLIPPERVAULT_LIST_PAGE_SIZE", "200"), default=200)),
        last_activity_monotonic=time.monotonic(),
        duplicate_compare_view=MacroCompareView(),
    )
    _print_state_refresh_inflight = False

    # ── Top toolbar ──────────────────────────────────────────────────────────
    with ui.header().classes("items-center gap-2 px-4 py-2 bg-grey-9 flex-wrap"):
        ui.label(t("Klipper Vault")).classes("text-xl font-bold text-white")
        ui.space()
        active_printer_select = (
            ui.select(options=[], label=t("Active printer"))
            .props("outlined dense options-dense")
            .classes("w-64 min-w-[14rem]")
        )
        with ui.button(t("Printers"), icon="print").props("flat color=white") as printers_menu_button:
            printers_menu_button.set_visibility(off_printer_mode_enabled)
            with ui.menu():
                off_printer_manage_profiles_button = ui.menu_item(t("Manage printer connections"))
                off_printer_test_button = ui.menu_item(t("Test printer connection"))
        with ui.button(t("Macro actions"), icon="menu").props("flat color=white") as macro_actions_button:
            state.macro_actions_button = macro_actions_button
            with ui.menu() as macro_actions_menu:
                state.macro_actions_menu = macro_actions_menu
                pass
        developer_menu: ui.menu | None = None
        if vault_cfg.developer:
            with ui.button(t("Developer"), icon="developer_mode").props("flat color=white"):
                with ui.menu() as developer_menu:
                    state.developer_menu = developer_menu
                    pass
        reload_dynamic_macros_button = ui.button(t("Reload Dynamic Macros"), icon="autorenew").props("flat color=white")
        reload_dynamic_macros_button.classes("text-blue-4")
        reload_dynamic_macros_button.set_visibility(False)
        state.reload_dynamic_macros_button = reload_dynamic_macros_button
        
        restart_klipper_button = ui.button(t("Restart Klipper"), icon="restart_alt").props("flat color=white")
        restart_klipper_button.classes("text-orange-4")
        restart_klipper_button.set_visibility(False)
        state.restart_klipper_button = restart_klipper_button
        
        duplicate_warning_button = ui.button(t("Duplicates found"), icon="warning").props("flat no-caps")
        duplicate_warning_button.classes("text-yellow-5")
        duplicate_warning_button.set_visibility(False)
        state.duplicate_warning_button = duplicate_warning_button
        
        save_config_button = ui.button(t("Save Config"), icon="cloud_upload").props("flat color=white")
        state.save_config_button = save_config_button
        
        index_button = ui.button(t("Scan macros"), icon="search").props("flat color=white")
        state.index_button = index_button

        settings_toolbar_button = ui.button(icon="settings").props("flat round color=white")
        settings_toolbar_button.tooltip(t("Settings"))

    # All state is now managed through the UIState container.
    # Callbacks access state directly via closure capture of the state object.

    def _note_activity() -> None:
        """Record runtime activity for UI/background flow control."""
        state.last_activity_monotonic = time.monotonic()

    def flat_dialog_button(label_key: str, on_click) -> None:
        """Render a standard flat no-caps dialog action button."""
        ui.button(t(label_key), on_click=on_click).props("flat no-caps")

    async def _on_import_upload(e) -> None:
        """Capture uploaded macro share file contents for import."""
        uploaded_file = getattr(e, "file", None)
        if uploaded_file is None:
            state.uploaded_import_bytes = None
            state.uploaded_import_name = ""
            state.import_error_label.set_text(t("Please upload a macro share file."))
            return

        state.uploaded_import_bytes = await uploaded_file.read()
        state.uploaded_import_name = str(getattr(uploaded_file, "name", "") or "")
        state.import_error_label.set_text("")

    def _refresh_reload_buttons() -> None:
        """Show exactly one pending reload action button when printer is idle."""
        is_allowed = (not state.printer_is_printing) and (not state.printer_is_busy)
        show_restart = state.restart_required and is_allowed
        # Dynamic macros can be reloaded while printing.
        show_dynamic_reload = (not state.restart_required) and state.dynamic_reload_required

        if state.restart_klipper_button:
            state.restart_klipper_button.set_enabled(show_restart)
            state.restart_klipper_button.set_visibility(show_restart)

        if state.reload_dynamic_macros_button:
            state.reload_dynamic_macros_button.set_enabled(show_dynamic_reload)
            state.reload_dynamic_macros_button.set_visibility(show_dynamic_reload)

    def _refresh_save_config_button() -> None:
        """Enable Save Config only when local changes are pending and printer is idle."""
        if state.save_config_button is None:
            return
        is_ready = _remote_actions_available()
        can_upload_now = is_ready and (not state.printer_is_printing)
        enabled = can_upload_now and state.has_unsynced_local_changes
        state.save_config_button.set_enabled(enabled)

    def _mark_local_changes_pending() -> None:
        """Track that local cfg changes must be explicitly uploaded."""
        state.has_unsynced_local_changes = True
        _refresh_save_config_button()

    def _mark_local_changes_saved() -> None:
        """Clear pending local-change sync state after explicit remote upload."""
        state.has_unsynced_local_changes = False
        _refresh_save_config_button()

    def _remote_actions_available() -> bool:
        """Return True when backend actions are currently available."""
        off_printer_ready = (not off_printer_mode_enabled) or state.off_printer_profile_ready
        return off_printer_ready

    def _remote_sync_status_suffix(result: dict[str, object]) -> str:
        """Build a compact status suffix for off-printer remote sync metadata."""
        if not off_printer_mode_enabled or not isinstance(result, dict):
            return ""

        remote_sync = result.get("remote_sync")
        if isinstance(remote_sync, dict):
            uploaded = _to_int(remote_sync.get("uploaded_files", 0), default=0)
            removed = _to_int(remote_sync.get("removed_remote_files", 0), default=0)
            fetched = _to_int(remote_sync.get("synced_files", 0), default=0)
            blocked = _to_int(remote_sync.get("blocked_files", 0), default=0)
            if uploaded > 0 or removed > 0:
                suffix = " | " + t("Remote sync: {uploaded} uploaded, {removed} removed", uploaded=uploaded, removed=removed)
                if blocked > 0:
                    suffix += " | " + t("Protected file skipped")
                return suffix
            if fetched > 0:
                return " | " + t("Remote sync: {fetched} fetched", fetched=fetched)
            if blocked > 0:
                return " | " + t("Protected file skipped")

        uploaded_paths = result.get("remote_uploaded_paths")
        if isinstance(uploaded_paths, list) and uploaded_paths:
            return " | " + t("Remote sync: {count} uploaded", count=len(uploaded_paths))

        remote_path = str(result.get("remote_path", "")).strip()
        if remote_path:
            return " | " + t("Remote updated")

        if bool(result.get("remote_synced", False)):
            suffix = " | " + t("Remote sync complete")
            restart_message = str(result.get("restart_message", "")).strip()
            if restart_message:
                suffix += " | " + restart_message
            return suffix

        restart_message = str(result.get("restart_message", "")).strip()
        if restart_message:
            return " | " + restart_message
        return ""

    def _is_remote_conflict_error(error: Exception | str) -> bool:
        """Return True when an error indicates stale remote cfg state."""
        text = str(error or "").lower()
        return "remote cfg conflict" in text

    def _show_remote_conflict_guidance(
        *,
        operation_label: str,
        error: Exception | str,
        local_error_label: ui.label | None = None,
    ) -> bool:
        """Show actionable remote conflict guidance and return True when handled."""
        if not _is_remote_conflict_error(error):
            return False

        error_text = str(error or "").strip()
        guidance = t(
            "Remote configuration changed while processing '{operation}'. Sync remote cfg files, review differences, and retry.",
            operation=operation_label,
        )
        if local_error_label is not None:
            local_error_label.set_text(guidance)

        state.status_label.set_text(guidance)
        remote_conflict_dialog_detail.set_text(error_text)
        remote_conflict_dialog_guidance.set_text(guidance)
        remote_conflict_dialog.open()
        return True

    def _set_off_printer_profile_state(ready: bool, detail: str = "") -> None:
        """Update off-printer profile status indicators."""
        state.off_printer_profile_ready = ready
        detail_text = str(detail or "").strip()
        label = state.off_printer_profile_label
        if label is None:
            return
        if ready:
            state.off_printer_profile_status_text = t("Printer connection ready")
            if detail_text:
                state.off_printer_profile_status_text = t("Printer connection ready: {detail}", detail=detail_text)
            label.classes(replace="text-xs text-positive")
        else:
            state.off_printer_profile_status_text = t("No active printer connection configured")
            if detail_text:
                state.off_printer_profile_status_text = t(
                    "No active printer connection configured: {detail}",
                    detail=detail_text,
                )
            label.classes(replace="text-xs text-negative")
        label.set_text(state.off_printer_profile_status_text)
        _refresh_save_config_button()

    def refresh_off_printer_profile_state() -> None:
        """Refresh off-printer profile readiness state from local profile storage."""
        if not off_printer_mode_enabled:
            return
        was_ready = state.off_printer_profile_ready
        try:
            profile = service.get_active_ssh_profile()
        except Exception as exc:
            _set_off_printer_profile_state(False, str(exc))
            return

        if not isinstance(profile, dict) or not profile:
            _set_off_printer_profile_state(False)
            return

        profile_name = str(profile.get("profile_name", "")).strip() or t("unnamed")
        auth_mode = str(profile.get("auth_mode", "")).strip().lower()
        has_secret = bool(profile.get("has_secret", False))
        if auth_mode in {"password", "key"} and not has_secret:
            _set_off_printer_profile_state(False, t("{profile} (missing credentials)", profile=profile_name))
            return

        _set_off_printer_profile_state(True, profile_name)
        if state.printer_state == "unknown" and state.off_printer_profile_label is not None:
            detail = str(state.printer_status_message or "").strip()
            offline_text = t("Printer offline")
            if detail:
                offline_text = t("Printer offline: {detail}", detail=detail)
            state.off_printer_profile_label.classes(replace="text-xs text-negative")
            state.off_printer_profile_label.set_text(offline_text)
        if not was_ready and state.off_printer_profile_ready:
            _maybe_run_deferred_startup_scan("printer connection became ready")

    def _mark_reload_required(*, is_dynamic: bool = False) -> None:
        """Mark pending runtime action after macro-affecting changes."""
        if is_dynamic and not state.restart_required:
            state.dynamic_reload_required = True
        else:
            state.restart_required = True
            state.dynamic_reload_required = False
        _refresh_reload_buttons()

    def _clear_restart_required() -> None:
        """Clear pending runtime action after successful restart/reload."""
        state.restart_required = False
        state.dynamic_reload_required = False
        _refresh_reload_buttons()

    def _is_dynamic_version_row(version_row: dict[str, object]) -> bool:
        """Return True when selected macro version is sourced from dynamic configs."""
        return bool(version_row.get("is_dynamic", False))

    def _files_include_dynamic_macros(paths: list[str]) -> bool:
        """Return True when any touched cfg path maps to known dynamic macros."""
        if not paths:
            return False

        dynamic_files = {
            str(macro.get("file_path", ""))
            for macro in state.cached_macros
            if bool(macro.get("is_dynamic", False))
        }
        if not dynamic_files:
            return False

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

        for dynamic_file in dynamic_files:
            if dynamic_file in normalized:
                return True
            if Path(dynamic_file).name in normalized:
                return True
        return False

    with ui.dialog().props("persistent") as printer_connecting_dialog, ui.card().classes("w-[30rem] max-w-[94vw]"):
        ui.label(t("Connecting to printer...")).classes("text-lg font-semibold")
        printer_connecting_label = ui.label(
            t("KlipperVault is reconnecting. The dialog closes automatically when the UI is responsive again.")
        ).classes("text-sm text-grey-5")
        state.printer_connecting_dialog = printer_connecting_dialog
        state.printer_connecting_label = printer_connecting_label

    def _set_printer_connecting_modal(visible: bool, detail: str = "") -> None:
        """Show a modal while reconnecting to printer and hide when healthy again."""
        if visible:
            detail_text = str(detail or "").strip()
            if detail_text:
                state.printer_connecting_label.set_text(
                    t(
                        "KlipperVault is reconnecting. The dialog closes automatically when the UI is responsive again."
                    )
                    + "\n"
                    + detail_text
                )
            else:
                state.printer_connecting_label.set_text(
                    t("KlipperVault is reconnecting. The dialog closes automatically when the UI is responsive again.")
                )
            if not state.printer_connecting_modal_open:
                state.printer_connecting_dialog.open()
                state.printer_connecting_modal_open = True
            return

        if state.printer_connecting_modal_open:
            state.printer_connecting_dialog.close()
            state.printer_connecting_modal_open = False

    with ui.dialog().props("persistent") as file_operation_dialog, ui.card().classes("w-[34rem] max-w-[96vw]"):
        file_operation_title = ui.label(t("Working on files")).classes("text-lg font-semibold")
        file_operation_phase = ui.label("").classes("text-sm text-grey-5")
        file_operation_percent = ui.label("0%").classes("text-sm text-grey-5 mt-1")
        file_operation_progress = ui.linear_progress(value=0.0, show_value=False).classes("w-full mt-1")

    def _file_operation_phase_text(phase: str) -> str:
        """Map backend phase keys to user-facing file-operation text."""
        normalized = str(phase or "").strip().lower()
        if normalized == "download":
            return t("Downloading cfg files from printer...")
        if normalized == "upload":
            return t("Uploading changed cfg files to printer...")
        if normalized == "parse":
            return t("Parsing local cfg files...")
        return t("Working on files...")

    def _set_file_operation_progress(phase: str, current: int, total: int) -> None:
        """Update blocking file-operation modal progress in percent."""
        display_total = max(int(total), 1)
        display_current = min(max(int(current), 0), display_total)
        value = display_current / display_total
        percent = int(round(value * 100.0))
        file_operation_phase.set_text(_file_operation_phase_text(phase))
        file_operation_percent.set_text(t("{percent}%", percent=percent))
        file_operation_progress.value = value
        file_operation_progress.update()

    def _run_with_file_operation_modal(title_text: str, action):
        """Run one sync file operation while showing a blocking progress modal."""
        file_operation_title.set_text(str(title_text))
        _set_file_operation_progress("", 0, 1)
        file_operation_dialog.open()
        try:
            return action()
        finally:
            file_operation_dialog.close()

    with ui.dialog().props("persistent") as printer_profile_dialog, ui.card().classes("w-[34rem] max-w-[96vw]"):
        ui.label(t("Printer identity")).classes("text-lg font-semibold")
        ui.label(t("Automatic detection could not determine vendor/model. Please enter them.")).classes("text-sm text-grey-5")
        printer_vendor_input = ui.input(label=t("Printer vendor")).props("outlined autofocus").classes("w-full mt-2")
        printer_model_input = ui.input(label=t("Printer model")).props("outlined").classes("w-full mt-2")
        printer_profile_error = ui.label("").classes("text-sm text-negative mt-1")
        with ui.row().classes("w-full justify-end gap-2 mt-3"):
            save_printer_profile_button = ui.button(t("Save")).props("color=primary no-caps")
    state.printer_profile_dialog = printer_profile_dialog
    state.printer_vendor_input = printer_vendor_input
    state.printer_model_input = printer_model_input
    state.printer_profile_error = printer_profile_error
    state.save_printer_profile_button = save_printer_profile_button

    with ui.dialog() as app_settings_dialog, ui.card().classes("w-[42rem] max-w-[96vw]"):
        ui.label(t("Application settings")).classes("text-lg font-semibold")
        ui.label(t("Configure KlipperVault settings stored in the local database.")).classes("text-sm text-grey-5")
        settings_version_history_input = (
            ui.number(label=t("Version history size"), value=vault_cfg.version_history_size)
            .props("outlined dense")
            .classes("w-full mt-2")
        )
        settings_language_select = (
            ui.select(
                options={"en": "English", "de": "Deutsch", "fr": "Francais"},
                value=str(vault_cfg.ui_language or "en"),
                label=t("UI language"),
            )
            .props("outlined dense")
            .classes("w-full")
        )
        settings_repo_url_input = ui.input(label=t("Online update repository URL")).props("outlined dense").classes("w-full")
        settings_manifest_input = ui.input(label=t("Online update manifest path")).props("outlined dense").classes("w-full")
        settings_ref_input = ui.input(label=t("Online update reference")).props("outlined dense").classes("w-full")
        settings_developer_toggle = ui.switch(t("Developer mode"), value=bool(vault_cfg.developer))
        settings_error_label = ui.label("").classes("text-sm text-negative mt-1")
        settings_info_label = ui.label("").classes("text-sm text-grey-5 mt-1")
        with ui.row().classes("w-full justify-end gap-2 mt-3"):
            flat_dialog_button("Cancel", app_settings_dialog.close)
            save_settings_button = ui.button(t("Save")).props("color=primary no-caps")

    def _printer_profile_missing() -> bool:
        """Return True when active printer vendor/model values are not set."""
        vendor, model = _active_printer_identity()
        return not vendor or not model

    def _active_printer_identity() -> tuple[str, str]:
        """Return active printer vendor/model from current active printer profile."""
        profile = service.get_active_printer_profile()
        if isinstance(profile, dict) and profile:
            vendor = str(profile.get("vendor", "")).strip()
            model = str(profile.get("model", "")).strip()
            if vendor and model:
                return vendor, model
        return "", ""

    def refresh_printer_profile_selector() -> None:
        """Refresh active-printer selector options from service state."""
        state.printer_profile_option_ids.clear()
        try:
            profiles = service.list_printer_profiles()
        except Exception as exc:
            status_label.set_text(t("Failed to load printer profiles: {error}", error=exc))
            return

        has_non_default_profile = any(
            isinstance(raw, dict) and str(raw.get("profile_name", "")).strip() and str(raw.get("profile_name", "")).strip() != "Default Printer"
            for raw in profiles
        )

        options: list[str] = []
        selected = ""
        for raw in profiles:
            if not isinstance(raw, dict):
                continue
            profile_id = _to_int(raw.get("id"), default=0)
            if profile_id <= 0:
                continue
            profile_name = str(raw.get("profile_name", "")).strip() or t("unnamed")
            if has_non_default_profile and profile_name == "Default Printer":
                continue
            vendor = str(raw.get("vendor", "")).strip()
            model = str(raw.get("model", "")).strip()
            meta = f"{vendor} {model}".strip()
            active_suffix = " *" if bool(raw.get("is_active", False)) else ""
            label = f"{profile_name} [{meta}]" if meta else profile_name
            option = f"{label}{active_suffix}"
            options.append(option)
            state.printer_profile_option_ids[option] = profile_id
            if bool(raw.get("is_active", False)):
                selected = option

        if not selected and options:
            selected = options[0]
        active_printer_select.set_options(options, value=selected)

    def on_active_printer_profile_change(_event) -> None:
        """Switch active printer profile and refresh scoped data."""
        selected_option = str(active_printer_select.value or "").strip()
        profile_id = state.printer_profile_option_ids.get(selected_option, 0)
        if profile_id <= 0:
            return
        try:
            result = service.activate_printer_profile(profile_id)
        except Exception as exc:
            message = t("Failed to activate printer profile: {error}", error=exc)
            status_label.set_text(message)
            ui.notify(message, type="negative")
            return
        if not bool(result.get("ok", False)):
            message = t("Failed to activate printer profile.")
            status_label.set_text(message)
            ui.notify(message, type="warning")
            return

        refresh_printer_profile_selector()
        if off_printer_mode_enabled:
            refresh_off_printer_profile_state()
        refresh_print_state()
        refresh_data()
        message = t("Active printer profile updated.")
        status_label.set_text(message)
        ui.notify(message, type="positive")

    def _save_printer_profile() -> None:
        """Validate and persist active printer profile identity."""
        vendor = str(state.printer_vendor_input.value or "").strip()
        model = str(state.printer_model_input.value or "").strip()
        if not vendor or not model:
            state.printer_profile_error.set_text(t("Vendor and model are required."))
            return

        result = service.update_active_printer_identity(vendor=vendor, model=model)
        if not bool(result.get("ok", False)):
            state.printer_profile_error.set_text(t("Failed to save printer profile."))
            return
        refresh_printer_profile_selector()
        state.printer_profile_error.set_text("")
        state.printer_profile_dialog.close()

    state.save_printer_profile_button.on_click(_save_printer_profile)

    def open_app_settings_dialog() -> None:
        """Open settings dialog populated from current persisted app config."""
        settings_version_history_input.set_value(int(vault_cfg.version_history_size))
        settings_language_select.set_value(str(vault_cfg.ui_language or "en").strip().lower() or "en")
        settings_repo_url_input.set_value(str(vault_cfg.online_update_repo_url or "").strip())
        settings_manifest_input.set_value(str(vault_cfg.online_update_manifest_path or "").strip())
        settings_ref_input.set_value(str(vault_cfg.online_update_ref or "").strip())
        settings_developer_toggle.set_value(bool(vault_cfg.developer))
        settings_error_label.set_text("")
        settings_info_label.set_text(t("UI language changes apply immediately. Developer mode still requires app restart."))
        app_settings_dialog.open()

    def save_app_settings_dialog() -> None:
        """Validate and persist app settings in the SQLite configuration store."""
        version_history_size = _to_int(settings_version_history_input.value, default=0)
        ui_language = str(settings_language_select.value or "").strip().lower()
        repo_url = str(settings_repo_url_input.value or "").strip()
        manifest_path = str(settings_manifest_input.value or "").strip()
        update_ref = str(settings_ref_input.value or "").strip()
        developer_mode = bool(settings_developer_toggle.value)

        if version_history_size < 1:
            settings_error_label.set_text(t("Version history size must be at least 1."))
            return
        if ui_language not in {"en", "de", "fr"}:
            settings_error_label.set_text(t("Unsupported UI language."))
            return
        if not manifest_path:
            settings_error_label.set_text(t("Online update manifest path is required."))
            return
        if not update_ref:
            settings_error_label.set_text(t("Online update reference is required."))
            return

        language_changed = str(vault_cfg.ui_language or "en").strip().lower() != ui_language
        restart_required = bool(vault_cfg.developer) != developer_mode

        new_cfg = VaultConfig(
            version_history_size=version_history_size,
            port=10090,
            runtime_mode="off_printer",
            ui_language=ui_language,
            printer_vendor=str(vault_cfg.printer_vendor or "").strip(),
            printer_model=str(vault_cfg.printer_model or "").strip(),
            online_update_repo_url=repo_url,
            online_update_manifest_path=manifest_path,
            online_update_ref=update_ref,
            developer=developer_mode,
        )

        try:
            _save_vault_config(config_dir, new_cfg, db_path)
        except Exception as exc:
            settings_error_label.set_text(t("Failed to save settings: {error}", error=exc))
            return

        # Update in-memory values so runtime operations pick up new settings immediately.
        vault_cfg.version_history_size = int(new_cfg.version_history_size)
        vault_cfg.ui_language = str(new_cfg.ui_language)
        vault_cfg.online_update_repo_url = str(new_cfg.online_update_repo_url)
        vault_cfg.online_update_manifest_path = str(new_cfg.online_update_manifest_path)
        vault_cfg.online_update_ref = str(new_cfg.online_update_ref)
        vault_cfg.developer = bool(new_cfg.developer)
        service.set_version_history_size(vault_cfg.version_history_size)

        app_settings_dialog.close()
        if language_changed:
            set_language(vault_cfg.ui_language)
            # Rebuild all translated labels in the active client without restarting backend runtime.
            ui.run_javascript("window.location.reload()")
            return

        if restart_required:
            state.status_label.set_text(t("Settings saved. Restart KlipperVault to apply all changes."))
        else:
            state.status_label.set_text(t("Settings saved."))

    save_settings_button.on_click(save_app_settings_dialog)

    with ui.grid().classes("w-full grid-cols-1 md:grid-cols-3 xl:grid-cols-4 gap-4 p-4 xl:h-[calc(100vh-110px)]"):
        with ui.card().classes("col-span-1 xl:h-full flex flex-col overflow-hidden min-h-[55vh] xl:min-h-0"):
            ui.label(t("Indexed macros")).classes("text-lg font-semibold mb-2 shrink-0")
            search_input = ui.input(placeholder=t("Search macros…")).props("clearable dense outlined").classes("w-full mb-1 shrink-0")
            with ui.row().classes("items-center gap-2 mb-1 shrink-0"):
                duplicates_button = ui.button(t("Show duplicates")).props("flat dense no-caps")
                new_button = ui.button(t("Show new")).props("flat dense no-caps")
                active_filter_button = ui.button(t("Filter: {state}", state=t("all"))).props("flat dense no-caps")
            with ui.row().classes("items-center gap-1 mb-1 shrink-0"):
                ui.label(t("Sort:")).classes("text-xs text-grey-4")
                sort_radio = (
                    ui.radio(
                        options={"load_order": t("Load order"), "alpha_asc": "A → Z", "alpha_desc": "Z → A"},
                        value="load_order",
                    )
                    .props("inline dense")
                    .classes("text-xs")
                )
            macro_count_label = ui.label(t("Items: {visible}", visible=0)).classes("text-sm text-grey-4 shrink-0")
            with ui.row().classes("items-center gap-2 mb-1 shrink-0"):
                prev_page_button = ui.button(t("Prev"))
                prev_page_button.props("flat dense no-caps")
                next_page_button = ui.button(t("Next"))
                next_page_button.props("flat dense no-caps")
            macro_list = ui.list().props("separator").classes("w-full overflow-y-auto flex-1 min-h-0")
            state.macro_search = search_input
            state.macro_list = macro_list

        viewer = MacroViewer()
        state.viewer = viewer

        with ui.card().classes("col-span-1 md:col-span-3 xl:col-span-1 xl:h-full overflow-auto"):
            ui.label(t("Stored macro statistics")).classes("text-lg font-semibold")
            with ui.column().classes("gap-2 mt-2"):
                total_macros_label = ui.label(t("Total macros: {count}", count=0))
                duplicate_macros_label = ui.label(t("Duplicate macros: {count}", count=0))
                deleted_macros_label = ui.label(t("Deleted macros: {count}", count=0))
                distinct_files_label = ui.label(t("Config files: {count}", count=0))
                last_update_label = ui.label(t("Last update: never"))
                purge_deleted_button = ui.button(t("Remove all deleted macros")).props(
                    "flat color=negative no-caps"
                )
                purge_deleted_button.set_visibility(False)

            ui.separator().classes("my-2")
            ui.label(t("Status")).classes("text-md font-semibold mb-1")
            status_label = ui.label(t("Ready")).classes("text-sm text-grey-4")
            state.status_label = status_label
            off_printer_profile_label = ui.label("").classes("text-xs text-grey-5")
            off_printer_profile_label.set_visibility(off_printer_mode_enabled)
            state.off_printer_profile_label = off_printer_profile_label
            with ui.row().classes("items-center gap-2"):
                off_printer_cfg_list_button = ui.button(t("Show remote cfg files")).props("flat dense no-caps")
                off_printer_cfg_list_button.set_visibility(off_printer_mode_enabled)
            ui.separator().classes("my-2")
            ui.label(t("Backups")).classes("text-md font-semibold mb-1")
            backup_list = ui.list().props("separator").classes("w-full max-h-[46vh] overflow-y-auto")

    with ui.dialog() as backup_dialog, ui.card().classes("w-[34rem] max-w-[96vw]"):
        ui.label(t("Create macro backup")).classes("text-lg font-semibold")
        ui.label(t("Store a named snapshot of the latest state of all macros.")).classes("text-sm text-grey-5")
        backup_name_input = ui.input(label=t("Backup name")).props("outlined autofocus").classes("w-full mt-2")
        backup_error_label = ui.label("").classes("text-sm text-negative mt-1")
        with ui.row().classes("w-full justify-end gap-2 mt-3"):
            flat_dialog_button("Cancel", backup_dialog.close)
            create_backup_button = ui.button(t("Create backup")).props("color=primary no-caps")
    state.backup_dialog = backup_dialog

    with ui.dialog() as backup_view_dialog, ui.card().classes("w-[74rem] max-w-[98vw] h-[86vh] max-h-[94vh] flex flex-col"):
        backup_view_title = ui.label(t("Backup contents")).classes("text-lg font-semibold")
        backup_view_subtitle = ui.label("").classes("text-sm text-grey-5")
        backup_view_table = ui.table(
            columns=[
                {"name": "macro_name", "label": t("Macro"), "field": "macro_name", "align": "left"},
                {"name": "file_path", "label": t("Config file"), "field": "file_path", "align": "left"},
                {"name": "version", "label": t("Version"), "field": "version", "align": "right"},
                {"name": "status", "label": t("Status"), "field": "status", "align": "left"},
            ],
            rows=[],
            row_key="_row_id",
            pagination=40,
        ).classes("w-full flex-1 overflow-auto mt-2")
        with ui.row().classes("w-full justify-end mt-3"):
            flat_dialog_button("Close", backup_view_dialog.close)
    state.backup_view_dialog = backup_view_dialog

    with ui.dialog() as load_order_dialog, ui.card().classes(
        "w-[56rem] max-w-[98vw] h-[86vh] max-h-[94vh] flex flex-col overflow-hidden"
    ):
        with ui.row().classes("w-full items-center justify-between"):
            ui.button(icon="close", on_click=load_order_dialog.close).props("flat dense round")
            ui.label(t("Klipper loading order overview")).classes("text-lg font-semibold")
            ui.space()
        load_order_summary_label = ui.label("").classes("text-sm text-grey-5")
        ui.label(t("Klipper parse order")).classes("text-sm font-semibold mt-2")
        load_order_text = ui.label("").classes(
            "w-full flex-1 overflow-y-auto whitespace-pre-wrap break-words border border-grey-8 rounded p-3 font-mono text-sm mt-2"
        )

        with ui.row().classes("w-full items-center mt-3"):
            ui.space()
            flat_dialog_button("Close", load_order_dialog.close)
    state.load_order_dialog = load_order_dialog

    with ui.dialog() as restore_dialog, ui.card().classes("w-[30rem] max-w-[96vw]"):
        ui.label(t("Restore backup")).classes("text-lg font-semibold")
        restore_confirm_label = ui.label("").classes("text-sm text-grey-5")
        restore_error_label = ui.label("").classes("text-sm text-negative mt-1")
        with ui.row().classes("w-full justify-end gap-2 mt-3"):
            flat_dialog_button("Cancel", restore_dialog.close)
            confirm_restore_button = ui.button(t("Restore")).props("color=warning no-caps")
        state.restore_dialog = restore_dialog
        state.restore_confirm_label = restore_confirm_label
        state.restore_error_label = restore_error_label

    with ui.dialog() as delete_dialog, ui.card().classes("w-[30rem] max-w-[96vw]"):
        ui.label(t("Delete backup")).classes("text-lg font-semibold")
        delete_confirm_label = ui.label("").classes("text-sm text-grey-5")
        delete_error_label = ui.label("").classes("text-sm text-negative mt-1")
        with ui.row().classes("w-full justify-end gap-2 mt-3"):
            flat_dialog_button("Cancel", delete_dialog.close)
            confirm_delete_button = ui.button(t("Delete")).props("color=negative no-caps")
        state.delete_dialog = delete_dialog
        state.delete_confirm_label = delete_confirm_label
        state.delete_error_label = delete_error_label

    with ui.dialog() as export_dialog, ui.card().classes("w-[42rem] max-w-[98vw]"):
        ui.label(t("Export macros")).classes("text-lg font-semibold")
        ui.label(t("Select one or more macros to export into a share file.")).classes("text-sm text-grey-5")
        ui.label(t("Macros")).classes("text-sm mt-2")
        export_macro_list = ui.column().classes("w-full max-h-[20rem] overflow-y-auto gap-1 border rounded p-2")
        export_error_label = ui.label("").classes("text-sm text-negative mt-1")
        with ui.row().classes("w-full justify-end gap-2 mt-3"):
            flat_dialog_button("Cancel", export_dialog.close)
            confirm_export_button = ui.button(t("Export")).props("color=primary no-caps")
    state.export_dialog = export_dialog
    state.export_macro_list = export_macro_list

    export_macro_checkboxes: dict[str, object] = {}

    with ui.dialog() as import_dialog, ui.card().classes("w-[38rem] max-w-[98vw]"):
        ui.label(t("Import macros")).classes("text-lg font-semibold")
        ui.label(t("Import a shared macro file into inactive new versions.")).classes("text-sm text-grey-5")
        import_upload = ui.upload(on_upload=_on_import_upload, auto_upload=True).props("accept=.json").classes("w-full mt-2")
        import_error_label = ui.label("").classes("text-sm text-negative mt-1")
        with ui.row().classes("w-full justify-end gap-2 mt-3"):
            flat_dialog_button("Cancel", import_dialog.close)
            confirm_import_button = ui.button(t("Import")).props("color=primary no-caps")
    state.import_dialog = import_dialog
    state.import_uploader = import_upload
    state.import_error_label = import_error_label

    with ui.dialog() as create_pr_dialog, ui.card().classes("w-[46rem] max-w-[98vw]"):
        ui.label(t("Create Pull Request")).classes("text-lg font-semibold")
        ui.label(t("Publish active macros directly to GitHub and open a pull request.")).classes("text-sm text-grey-5")
        pr_repo_url_input = ui.input(label=t("Repository URL")).props("outlined").classes("w-full mt-2")
        with ui.row().classes("w-full gap-2"):
            pr_base_branch_input = ui.input(label=t("Base branch")).props("outlined").classes("w-full")
            pr_head_branch_input = ui.input(label=t("Head branch")).props("outlined").classes("w-full")
        pr_title_input = ui.input(label=t("Pull request title")).props("outlined").classes("w-full")
        pr_body_input = ui.textarea(label=t("Pull request description")).props("outlined autogrow").classes("w-full")
        pr_token_input = ui.input(label=t("GitHub API token")).props("outlined password password_toggle_button").classes("w-full")
        ui.label(t("Token is used only for this request and is not stored.")).classes("text-xs text-grey-5")
        ui.label(t("Required token permissions: Contents (write) and Pull requests (write)."))
        create_pr_progress_label = ui.label("").classes("text-sm text-grey-5 mt-2")
        create_pr_progress_bar = ui.linear_progress(value=0.0, show_value=False).classes("w-full mt-1")
        create_pr_progress_label.set_visibility(False)
        create_pr_progress_bar.set_visibility(False)
        create_pr_error_label = ui.label("").classes("text-sm text-negative mt-1")
        with ui.row().classes("w-full justify-end gap-2 mt-3"):
            flat_dialog_button("Cancel", create_pr_dialog.close)
            confirm_create_pr_button = ui.button(t("Create PR")).props("color=primary no-caps")
        state.create_pr_dialog = create_pr_dialog
        state.pr_repo_url_input = pr_repo_url_input
        state.pr_base_branch_input = pr_base_branch_input
        state.pr_head_branch_input = pr_head_branch_input
        state.pr_title_input = pr_title_input
        state.pr_body_input = pr_body_input
        state.pr_token_input = pr_token_input
        state.create_pr_error_label = create_pr_error_label
        state.confirm_create_pr_button = confirm_create_pr_button

    with ui.dialog() as online_update_dialog, ui.card().classes("w-[46rem] max-w-[98vw]"):
        ui.label(t("Online macro updates")).classes("text-lg font-semibold")
        ui.label(t("Changed macros are imported as new versions. Select which ones to activate now.")).classes("text-sm text-grey-5")
        online_update_progress_label = ui.label("").classes("text-sm text-grey-5 mt-2")
        online_update_progress_bar = ui.linear_progress(value=0.0, show_value=False).classes("w-full mt-1")
        online_update_progress_label.set_visibility(False)
        online_update_progress_bar.set_visibility(False)
        online_update_summary_label = ui.label("").classes("text-sm text-grey-5 mt-1")
        ui.label(t("Macros")).classes("text-sm mt-2")
        online_update_list = ui.column().classes("w-full max-h-[20rem] overflow-y-auto gap-1 border rounded p-2")
        online_update_error_label = ui.label("").classes("text-sm text-negative mt-1")
        with ui.row().classes("w-full justify-end gap-2 mt-3"):
            flat_dialog_button("Cancel", online_update_dialog.close)
            confirm_online_update_button = ui.button(t("Import updates")).props("color=primary no-caps")
            confirm_online_update_button.set_visibility(False)
        state.online_update_dialog = online_update_dialog
        state.online_update_list = online_update_list
        state.online_update_summary_label = online_update_summary_label
        state.online_update_error_label = online_update_error_label
        state.confirm_online_update_button = confirm_online_update_button

    with ui.dialog() as macro_delete_dialog, ui.card().classes("w-[30rem] max-w-[96vw]"):
        ui.label(t("Delete macro from cfg file")).classes("text-lg font-semibold")
        macro_delete_confirm_label = ui.label("").classes("text-sm text-grey-5")
        macro_delete_error_label = ui.label("").classes("text-sm text-negative mt-1")
        with ui.row().classes("w-full justify-end gap-2 mt-3"):
            flat_dialog_button("Cancel", macro_delete_dialog.close)
            confirm_macro_delete_button = ui.button(t("Delete")).props("color=negative no-caps")
        state.macro_delete_dialog = macro_delete_dialog
        state.macro_delete_confirm_label = macro_delete_confirm_label
        state.macro_delete_error_label = macro_delete_error_label

    with ui.dialog() as duplicate_wizard_dialog, ui.card().classes("w-[48rem] max-w-[98vw]"):
        duplicate_wizard_title = ui.label(t("Resolve duplicate macros")).classes("text-lg font-semibold")
        duplicate_wizard_subtitle = ui.label("").classes("text-sm text-grey-5")
        ui.separator().classes("my-2")
        duplicate_entry_list = ui.list().props("separator").classes("w-full max-h-[40vh] overflow-y-auto")
        duplicate_keep_select = (
            ui.select(options={}, label=t("Keep definition from"))
            .props("outlined dense")
            .classes("w-full mt-2")
        )
        with ui.row().classes("w-full items-end gap-2 mt-2"):
            duplicate_compare_with_select = (
                ui.select(options={}, label=t("Compare keep with"))
                .props("outlined dense")
                .classes("flex-1")
            )
            duplicate_compare_button = ui.button(t("Compare")).props("flat no-caps")
        duplicate_wizard_error = ui.label("").classes("text-sm text-negative mt-1")
        with ui.row().classes("w-full justify-between gap-2 mt-3"):
            duplicate_prev_button = ui.button(t("Previous")).props("flat no-caps")
            with ui.row().classes("gap-2"):
                flat_dialog_button("Cancel", duplicate_wizard_dialog.close)
                duplicate_next_button = ui.button(t("Next")).props("flat no-caps")
                duplicate_apply_button = ui.button(t("Apply")).props("color=warning no-caps")
        state.duplicate_wizard_dialog = duplicate_wizard_dialog
        state.duplicate_wizard_title = duplicate_wizard_title
        state.duplicate_wizard_subtitle = duplicate_wizard_subtitle
        state.duplicate_wizard_error = duplicate_wizard_error

    with ui.dialog() as remote_cfg_list_dialog, ui.card().classes("w-[52rem] max-w-[98vw] h-[82vh] max-h-[92vh] flex flex-col"):
        remote_cfg_list_title = ui.label(t("Remote cfg files")).classes("text-lg font-semibold")
        remote_cfg_list_subtitle = ui.label("").classes("text-sm text-grey-5")
        remote_cfg_list_text = ui.textarea(label=t("Files"), value="").props("readonly autogrow").classes(
            "w-full flex-1 mt-2"
        )
        remote_cfg_list_error = ui.label("").classes("text-sm text-negative mt-1")
        with ui.row().classes("w-full justify-end mt-3"):
            flat_dialog_button("Close", remote_cfg_list_dialog.close)

    with ui.dialog() as remote_conflict_dialog, ui.card().classes("w-[40rem] max-w-[96vw]"):
        ui.label(t("Remote changes detected")).classes("text-lg font-semibold text-warning")
        remote_conflict_dialog_guidance = ui.label("").classes("text-sm text-grey-4 mt-1")
        remote_conflict_dialog_detail = ui.label("").classes("text-sm text-negative mt-1")
        with ui.row().classes("w-full justify-end gap-2 mt-3"):
            flat_dialog_button("Close", remote_conflict_dialog.close)
            sync_after_conflict_button = ui.button(t("Sync and reload")).props("color=primary no-caps")

    with ui.dialog() as off_printer_profile_dialog, ui.card().classes("w-[44rem] max-w-[96vw]"):
        ui.label(t("Printer connection management")).classes("text-lg font-semibold")
        ui.label(t("Configure SSH settings as part of each printer profile for off-printer mode.")).classes("text-sm text-grey-5")
        ssh_profile_select = ui.select(options=[], label=t("Saved profiles")).props("outlined dense").classes("w-full mt-2")
        with ui.row().classes("w-full gap-2"):
            ssh_profile_name_input = ui.input(label=t("Profile name")).props("outlined dense").classes("flex-1")
            ssh_profile_host_input = ui.input(label=t("Host"), on_change=lambda e: _sync_moonraker_url_host(str(e.value or ""))).props("outlined dense").classes("flex-1")
        with ui.row().classes("w-full gap-2"):
            ssh_profile_port_input = ui.number(label=t("Port"), value=22).props("outlined dense").classes("w-32")
            ssh_profile_username_input = ui.input(label=t("Username")).props("outlined dense").classes("flex-1")
        ssh_profile_remote_dir_input = ui.input(label=t("Remote config directory"), value="~/printer_data/config").props(
            "outlined dense"
        ).classes("w-full")
        ssh_profile_moonraker_url_input = ui.input(
            label=t("Moonraker URL"), value="http://127.0.0.1:7125"
        ).props("outlined dense").classes("w-full")
        ssh_profile_auth_mode_select = ui.select(
            options={"key": t("SSH key"), "password": t("Password")},
            value="key",
            label=t("Authentication mode"),
        ).props("outlined dense").classes("w-full")
        ssh_profile_secret_input = ui.input(label=t("SSH secret")).props("outlined dense type=text").classes(
            "w-full"
        )
        ssh_profile_secret_mode_label = ui.label("").classes("text-xs text-grey-5")
        ssh_profile_secret_state_label = ui.label("").classes("text-xs text-grey-5")
        ssh_profile_active_toggle = ui.switch(t("Set as active profile"), value=True)
        ssh_profile_error_label = ui.label("").classes("text-sm text-negative mt-1")
        ssh_profile_status_label = ui.label("").classes("text-sm text-positive mt-1")
        with ui.row().classes("w-full justify-between mt-3"):
            with ui.row().classes("gap-2"):
                flat_dialog_button("Close", off_printer_profile_dialog.close)
                refresh_ssh_profiles_button = ui.button(t("Refresh")).props("flat no-caps")
                new_ssh_profile_button = ui.button(t("Add printer")).props("flat no-caps")
            with ui.row().classes("gap-2"):
                delete_ssh_profile_button = ui.button(t("Delete selected")).props("flat color=negative no-caps")
                activate_ssh_profile_button = ui.button(t("Activate selected")).props("flat no-caps")
                save_ssh_profile_button = ui.button(t("Save profile")).props("color=primary no-caps")

    def _sync_after_remote_conflict() -> None:
        """Close conflict dialog and run a one-click recovery sync/index."""
        remote_conflict_dialog.close()
        perform_index("remote conflict recovery")

    sync_after_conflict_button.on_click(_sync_after_remote_conflict)

    def open_macro_by_identity(file_path: str, macro_name: str) -> None:
        """Select a macro by identity, clearing filters if needed to reveal it."""
        # Ensure the active target is visible after link navigation.
        # If filters hide it, clear filters and search first.
        state.show_duplicates_only = False
        state.show_new_only = False
        state.active_filter = "all"
        state.search_query = ""
        state.macro_search.value = ""
        state.macro_search.update()
        update_duplicates_button_label()
        update_new_button_label()
        update_active_filter_button_label()

        for macro in state.cached_macros:
            if str(macro.get("file_path", "")) == file_path and str(macro.get("macro_name", "")) == macro_name:
                state.selected_key = macro_key(macro)
                break
        render_macro_list()

    state.viewer.set_open_macro_handler(open_macro_by_identity)

    def blocked_by_print_state(
        *,
        status_message: str,
        local_error_label: ui.label | None = None,
    ) -> bool:
        """Set consistent blocked messages when printer is currently printing."""
        if not state.printer_is_printing:
            return False
        if local_error_label is not None:
            local_error_label.set_text(t("Blocked while printer is printing."))
        state.status_label.set_text(t(status_message))
        return True

    def remove_deleted_macro_from_db(file_path: str, macro_name: str) -> None:
        """Permanently remove selected deleted macro from SQLite history."""
        if not file_path or not macro_name:
            status_label.set_text(t("Cannot remove deleted macro: missing identity."))
            return

        try:
            result = service.remove_deleted(file_path, macro_name)
        except Exception as exc:
            status_label.set_text(t("Failed to remove deleted macro: {error}", error=exc))
            return

        reason = str(result.get("reason", ""))
        removed = _to_int(result.get("removed", 0))
        if removed > 0:
            status_label.set_text(t(
                "Removed deleted macro '{macro_name}' from {file_path} ({removed} row(s)).",
                macro_name=macro_name,
                file_path=file_path,
                removed=removed,
            ))
        elif reason == "not_deleted":
            status_label.set_text(t("Selected macro is not marked deleted; nothing removed."))
        elif reason == "not_found":
            status_label.set_text(t("Macro not found in database."))
        else:
            status_label.set_text(t("No rows removed."))

        refresh_data()

    state.viewer.set_remove_deleted_handler(remove_deleted_macro_from_db)

    def remove_inactive_macro_from_db(version_row: dict) -> None:
        """Permanently remove selected inactive macro version from SQLite history."""
        file_path = str(version_row.get("file_path", ""))
        macro_name = str(version_row.get("macro_name", ""))
        version = _to_int(version_row.get("version", 0) or 0)
        if not file_path or not macro_name:
            state.status_label.set_text(t("Cannot remove inactive macro version: missing identity."))
            return

        try:
            result = service.remove_inactive_version(file_path, macro_name, version)
        except Exception as exc:
            state.status_label.set_text(t("Failed to remove inactive macro version: {error}", error=exc))
            return

        reason = str(result.get("reason", ""))
        removed = _to_int(result.get("removed", 0))
        if removed > 0:
            state.status_label.set_text(t(
                "Removed inactive macro version v{version} of '{macro_name}' from {file_path} ({removed} row(s)).",
                version=version,
                macro_name=macro_name,
                file_path=file_path,
                removed=removed,
            ))
            state.force_active_for_key = f"{file_path}::{macro_name}"
        elif reason == "not_inactive":
            state.status_label.set_text(t("Selected macro version is not inactive; nothing removed."))
        elif reason == "deleted":
            state.status_label.set_text(t("Selected macro version is deleted; use the deleted-macro removal action instead."))
        elif reason == "not_found":
            state.status_label.set_text(t("Macro not found in database."))
        else:
            state.status_label.set_text(t("No rows removed."))

        refresh_data()

    state.viewer.set_remove_inactive_handler(remove_inactive_macro_from_db)

    def restore_macro_version_from_viewer(version_row: dict) -> None:
        """Restore selected macro version into cfg file, then rescan."""
        file_path = str(version_row.get("file_path", ""))
        macro_name = str(version_row.get("macro_name", ""))
        version = _to_int(version_row.get("version", 0) or 0)
        is_deleted = bool(version_row.get("is_deleted", False))

        if not file_path or not macro_name or version <= 0:
            state.status_label.set_text(t("Cannot restore macro version: missing or invalid version data."))
            return

        try:
            result = service.restore_version(file_path, macro_name, version)
        except Exception as exc:
            if _show_remote_conflict_guidance(operation_label=t("macro restore"), error=exc):
                return
            state.status_label.set_text(t("Failed to restore macro version: {error}", error=exc))
            return

        action = t("Restored deleted macro") if is_deleted else t("Reverted macro")
        state.status_label.set_text(t(
            "{action} '{macro_name}' from {file_path} to v{version}. Local changes pending; click Save Config to upload.",
            action=action,
            macro_name=result["macro_name"],
            file_path=result["file_path"],
            version=result["version"],
        ))
        _mark_local_changes_pending()
        _mark_reload_required(is_dynamic=_is_dynamic_version_row(version_row))
        state.force_latest_for_key = f"{result['file_path']}::{result['macro_name']}"
        perform_index("macro restore", sync_remote=False)

    state.viewer.set_restore_version_handler(restore_macro_version_from_viewer)

    def save_macro_edit_from_viewer(version_row: dict, section_text: str) -> None:
        """Save edited macro text back into its source cfg file and re-index."""
        file_path = str(version_row.get("file_path", ""))
        macro_name = str(version_row.get("macro_name", ""))
        selected_version = _to_int(version_row.get("version", 0) or 0)
        if not file_path or not macro_name:
            raise ValueError("Cannot save macro: missing identity.")
        if bool(version_row.get("is_deleted", False)):
            raise ValueError("Cannot edit a deleted macro version.")

        latest_row = service.load_latest_for_file(macro_name, file_path)
        if latest_row is None:
            raise ValueError("Cannot save macro: latest version not found.")
        latest_version = _to_int(latest_row.get("version", 0) or 0)
        if selected_version != latest_version:
            raise ValueError("Only the latest macro version can be edited.")

        try:
            result = service.save_macro_editor_text(file_path, macro_name, section_text)
        except Exception as exc:
            _show_remote_conflict_guidance(operation_label=t("macro edit"), error=exc)
            raise

        state.status_label.set_text(t(
            "Saved macro '{macro_name}' in {file_path} ({operation}). Local changes pending; click Save Config to upload.",
            macro_name=result["macro_name"],
            file_path=result["file_path"],
            operation=result["operation"],
        ))
        _mark_local_changes_pending()
        _mark_reload_required(is_dynamic=_is_dynamic_version_row(version_row))
        state.force_latest_for_key = f"{result['file_path']}::{result['macro_name']}"
        perform_index("macro edit", sync_remote=False)

    state.viewer.set_save_macro_edit_handler(save_macro_edit_from_viewer)

    def _perform_delete_macro_source(version_row: dict) -> None:
        """Delete selected macro section from cfg file and re-index."""
        file_path = str(version_row.get("file_path", ""))
        macro_name = str(version_row.get("macro_name", ""))
        selected_version = _to_int(version_row.get("version", 0) or 0)

        if not file_path or not macro_name:
            raise ValueError(t("Cannot delete macro from cfg: missing identity."))
        if bool(version_row.get("is_deleted", False)):
            raise ValueError(t("Cannot delete a deleted macro version."))

        latest_row = service.load_latest_for_file(macro_name, file_path)
        if latest_row is None:
            raise ValueError(t("Cannot delete macro from cfg: latest version not found."))

        latest_version = _to_int(latest_row.get("version", 0) or 0)
        if selected_version != latest_version:
            raise ValueError(t("Only the latest macro version can be deleted from cfg."))

        try:
            result = service.delete_macro_source(file_path, macro_name)
        except Exception as exc:
            raise ValueError(t("Failed to delete macro from cfg: {error}", error=exc)) from exc

        removed = _to_int(result.get("removed_sections", 0))
        if removed <= 0:
            raise ValueError(t("Macro section not found in cfg file."))

        state.status_label.set_text(t(
            "Deleted macro '{macro_name}' from {file_path} ({removed} section(s)). Local changes pending; click Save Config to upload.",
            macro_name=result["macro_name"],
            file_path=result["file_path"],
            removed=removed,
        ))
        _mark_local_changes_pending()
        _mark_reload_required(is_dynamic=_is_dynamic_version_row(version_row))
        state.force_latest_for_key = f"{result['file_path']}::{result['macro_name']}"
        perform_index("macro delete", sync_remote=False)

    def delete_macro_source_from_viewer(version_row: dict) -> None:
        """Open confirmation dialog before deleting selected macro from cfg."""
        file_path = str(version_row.get("file_path", ""))
        macro_name = str(version_row.get("macro_name", ""))
        state.macro_delete_target = version_row
        state.macro_delete_error_label.set_text("")
        state.macro_delete_confirm_label.set_text(t(
            "Delete macro '{macro_name}' from {file_path}? This removes it from the cfg file. It can still be restored from the vault until it is permanently removed.",
            macro_name=macro_name or "-",
            file_path=file_path or "-",
        ))
        state.macro_delete_dialog.open()

    def confirm_macro_delete() -> None:
        """Execute confirmed macro deletion from the viewer dialog."""
        if state.macro_delete_target is None:
            state.macro_delete_error_label.set_text(t("Selected entry data is not available."))
            return

        try:
            _perform_delete_macro_source(state.macro_delete_target)
        except Exception as exc:
            handled = _show_remote_conflict_guidance(
                operation_label=t("macro delete"),
                error=exc,
                local_error_label=state.macro_delete_error_label,
            )
            if not handled:
                state.macro_delete_error_label.set_text(str(exc))
            return

        state.macro_delete_dialog.close()
        state.macro_delete_target = None

    state.viewer.set_delete_macro_from_cfg_handler(delete_macro_source_from_viewer)

    def update_duplicates_button_label() -> None:
        """Sync duplicates filter button text with current filter state."""
        duplicates_button.set_text(t("Show all macros") if state.show_duplicates_only else t("Show duplicates"))

    def update_new_button_label() -> None:
        """Sync new-macros filter button text with current filter state."""
        new_button.set_text(t("Show all macros") if state.show_new_only else t("Show new"))

    def _translated_active_filter_state() -> str:
        """Return localized active-filter state label for button text."""
        if state.active_filter == "active":
            return t("active")
        if state.active_filter == "inactive":
            return t("inactive")
        return t("all")

    def update_active_filter_button_label() -> None:
        """Sync active/inactive cycle button text with current filter state."""
        active_filter_button.set_text(t("Filter: {state}", state=_translated_active_filter_state()))

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

    def render_status_badge(status_key: str) -> None:
        """Render a status badge with centralized label/class mapping."""
        ui.label(t(status_key)).classes(_STATUS_BADGE_CLASSES[status_key])

    def on_sort_change(e) -> None:
        """Radio selection change handler for sort order."""
        state.sort_order = e.value
        render_macro_list()

    def _default_keep_file(entries: list[dict[str, object]]) -> str:
        """Choose default keep target, preferring currently active entry."""
        for entry in entries:
            if entry.get("is_active", False):
                return str(entry.get("file_path", ""))
        return str(entries[0].get("file_path", "")) if entries else ""

    def _load_latest_macro_for_file(macro_name: str, file_path: str) -> dict | None:
        """Load latest stored row for one macro definition file."""
        return service.load_latest_for_file(macro_name, file_path)

    def _update_duplicate_compare_choice(entries: list[dict[str, object]], keep_file: str) -> None:
        """Refresh compare-target select options for current wizard step."""
        macro_name = str(state.duplicate_wizard_groups[state.duplicate_wizard_index].get("macro_name", ""))
        compare_options = {
            str(entry.get("file_path", "")): str(entry.get("file_path", ""))
            for entry in entries
            if str(entry.get("file_path", "")) != keep_file
        }
        duplicate_compare_with_select.options = compare_options

        selected_compare = state.duplicate_compare_with_choices.get(macro_name)
        if not selected_compare or selected_compare not in compare_options:
            selected_compare = next(iter(compare_options), "")
            state.duplicate_compare_with_choices[macro_name] = selected_compare
        duplicate_compare_with_select.value = selected_compare
        duplicate_compare_with_select.update()

        duplicate_compare_button.set_enabled(bool(compare_options))

    def _render_duplicate_wizard_step() -> None:
        """Render one duplicate macro group in the wizard."""
        if not state.duplicate_wizard_groups:
            return

        group = state.duplicate_wizard_groups[state.duplicate_wizard_index]
        macro_name = str(group.get("macro_name", ""))
        entries = _as_dict_list(group.get("entries", []))

        duplicate_wizard_title.set_text(t("Resolve duplicates: {macro_name}", macro_name=macro_name))
        duplicate_wizard_subtitle.set_text(
            t(
                "Step {index} of {total}",
                index=state.duplicate_wizard_index + 1,
                total=len(state.duplicate_wizard_groups),
            )
        )

        duplicate_entry_list.clear()
        with duplicate_entry_list:
            for entry in entries:
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    ui.label(str(entry.get("file_path", "-"))).classes("flex-1 text-sm")
                    ui.label(f"v{entry.get('version', '-')}").classes("text-[11px] text-grey-5")
                    if entry.get("is_active", False):
                        render_status_badge(status_badge_key(entry))

        options = {
            str(entry.get("file_path", "")): str(entry.get("file_path", ""))
            for entry in entries
        }
        duplicate_keep_select.options = options

        selected_file = state.duplicate_keep_choices.get(macro_name)
        if not selected_file or selected_file not in options:
            selected_file = _default_keep_file(entries)
            state.duplicate_keep_choices[macro_name] = selected_file

        duplicate_keep_select.value = selected_file
        duplicate_keep_select.update()
        _update_duplicate_compare_choice(entries, selected_file)
        state.duplicate_wizard_error.set_text("")

        duplicate_prev_button.set_enabled(state.duplicate_wizard_index > 0)
        duplicate_next_button.set_visibility(state.duplicate_wizard_index < len(state.duplicate_wizard_groups) - 1)
        duplicate_apply_button.set_visibility(state.duplicate_wizard_index == len(state.duplicate_wizard_groups) - 1)

    def _on_duplicate_keep_change(e) -> None:
        """Persist selected keep target for current duplicate group."""
        if not state.duplicate_wizard_groups:
            return
        macro_name = str(state.duplicate_wizard_groups[state.duplicate_wizard_index].get("macro_name", ""))
        keep_file = str(e.value or "")
        state.duplicate_keep_choices[macro_name] = keep_file
        entries = _as_dict_list(state.duplicate_wizard_groups[state.duplicate_wizard_index].get("entries", []))
        _update_duplicate_compare_choice(entries, keep_file)

    def _on_duplicate_compare_with_change(e) -> None:
        """Persist selected compare target for current duplicate group."""
        if not state.duplicate_wizard_groups:
            return
        macro_name = str(state.duplicate_wizard_groups[state.duplicate_wizard_index].get("macro_name", ""))
        state.duplicate_compare_with_choices[macro_name] = str(e.value or "")

    def open_duplicate_pair_compare() -> None:
        """Open side-by-side compare view for currently selected duplicate pair."""
        if not state.duplicate_wizard_groups:
            state.duplicate_wizard_error.set_text(t("No duplicates loaded."))
            return

        group = state.duplicate_wizard_groups[state.duplicate_wizard_index]
        macro_name = str(group.get("macro_name", ""))
        keep_file = str(state.duplicate_keep_choices.get(macro_name, ""))
        compare_file = str(state.duplicate_compare_with_choices.get(macro_name, ""))
        if not keep_file or not compare_file:
            state.duplicate_wizard_error.set_text(t("Select two definitions to compare."))
            return
        if keep_file == compare_file:
            state.duplicate_wizard_error.set_text(t("Choose a different definition for comparison."))
            return

        keep_macro = _load_latest_macro_for_file(macro_name, keep_file)
        compare_macro = _load_latest_macro_for_file(macro_name, compare_file)
        if keep_macro is None or compare_macro is None:
            state.duplicate_wizard_error.set_text(t("Could not load one or both macro definitions."))
            return

        compare_versions = [
            {
                **keep_macro,
                "version": 2,
                "compare_label": f"{keep_file} (keep)",
            },
            {
                **compare_macro,
                "version": 1,
                "compare_label": compare_file,
            },
        ]
        state.duplicate_compare_view.set_macro({"macro_name": macro_name}, compare_versions)
        state.duplicate_compare_view.open()

    def open_duplicate_wizard() -> None:
        """Open duplicate-resolution wizard from toolbar warning button."""
        state.duplicate_wizard_groups = service.list_duplicates()
        if not state.duplicate_wizard_groups:
            state.status_label.set_text(t("No duplicates found."))
            return

        backup_name = datetime.now().strftime("Resolve_Duplicates-%Y%m%d-%H%M%S")
        try:
            backup_result = service.create_backup(backup_name)
        except Exception as exc:
            state.status_label.set_text(t("Failed to create pre-resolve backup: {error}", error=exc))
            return

        state.duplicate_keep_choices = {}
        state.duplicate_compare_with_choices = {}
        state.duplicate_wizard_index = 0
        _render_duplicate_wizard_step()
        state.duplicate_wizard_dialog.open()
        state.status_label.set_text(t(
            "Created pre-resolve backup '{backup_name}' with {macro_count} macro(s).",
            backup_name=backup_result["backup_name"],
            macro_count=backup_result["macro_count"],
        ))
        render_backup_list()

    def duplicate_wizard_previous() -> None:
        """Navigate to previous duplicate group."""
        if state.duplicate_wizard_index <= 0:
            return
        state.duplicate_wizard_index -= 1
        _render_duplicate_wizard_step()

    def duplicate_wizard_next() -> None:
        """Navigate to next duplicate group."""
        if state.duplicate_wizard_index >= len(state.duplicate_wizard_groups) - 1:
            return
        state.duplicate_wizard_index += 1
        _render_duplicate_wizard_step()

    def apply_duplicate_resolution() -> None:
        """Apply keep choices by deleting duplicate sections from cfg files."""
        if not state.duplicate_wizard_groups:
            state.duplicate_wizard_error.set_text(t("No duplicates loaded."))
            return

        missing = [
            str(group.get("macro_name", ""))
            for group in state.duplicate_wizard_groups
            if not state.duplicate_keep_choices.get(str(group.get("macro_name", "")))
        ]
        if missing:
            state.duplicate_wizard_error.set_text(t("Select a keep target for every macro before applying."))
            return

        keep_map = {
            str(group.get("macro_name", "")): str(state.duplicate_keep_choices[str(group.get("macro_name", ""))])
            for group in state.duplicate_wizard_groups
        }

        try:
            result = service.resolve_duplicates(keep_choices=keep_map, duplicate_groups=state.duplicate_wizard_groups)
        except Exception as exc:
            if _show_remote_conflict_guidance(
                operation_label=t("duplicate resolution"),
                error=exc,
                local_error_label=state.duplicate_wizard_error,
            ):
                return
            state.duplicate_wizard_error.set_text(t("Failed to resolve duplicates: {error}", error=exc))
            return

        state.duplicate_wizard_dialog.close()
        touched_files_raw = result.get("touched_files", [])
        touched_files_count = len(touched_files_raw) if isinstance(touched_files_raw, list) else 0
        state.status_label.set_text(t(
            "Removed {removed_sections} duplicate section(s) in {file_count} file(s). Local changes pending; click Save Config to upload.",
            removed_sections=result["removed_sections"],
            file_count=touched_files_count,
        ))
        _mark_local_changes_pending()
        touched_files = [str(path) for path in touched_files_raw] if isinstance(touched_files_raw, list) else []
        _mark_reload_required(is_dynamic=_files_include_dynamic_macros(touched_files))
        perform_index("duplicate wizard", sync_remote=False)

    def render_macro_list() -> None:
        """Render the left macro list with filters, badges, and selection state."""
        # Memory-trim/no-client recovery can release cached list rows.
        # If rows are known to exist, repopulate from DB before rendering.
        if (not state.cached_macros) and state.total_macro_rows > 0 and (not state.is_indexing):
            refresh_data()
            return

        state.macro_list.clear()
        state.viewer.set_available_macros(state.cached_macros)

        duplicate_names = state.cached_duplicate_names
        visible_macros = filter_macros(
            macros=state.cached_macros,
            search_query=state.search_query,
            show_duplicates_only=state.show_duplicates_only,
            active_filter=state.active_filter,
            duplicate_names=duplicate_names,
            show_new_only=state.show_new_only,
        )
        
        visible_macros = sort_macros(visible_macros, state.sort_order)
        if (
            state.sort_order == "load_order"
            and state.printer_is_printing
            and visible_macros
            and all(_to_int(m.get("load_order_index", 999999), default=999999) >= 999999 for m in visible_macros)
        ):
            # During active prints, remote parse-order metadata can be temporarily unavailable.
            # Fall back to deterministic file/line order so "Load order" still works.
            visible_macros = sorted(
                visible_macros,
                key=lambda m: (
                    0 if bool(m.get("is_loaded", True)) else 1,
                    str(m.get("file_path", "")),
                    _to_int(m.get("line_number", 999999), default=999999),
                    str(m.get("display_name") or m.get("macro_name", "")).lower(),
                ),
            )
        query = state.search_query.strip().lower()
        filter_active = bool(query) or state.show_duplicates_only or state.show_new_only or state.active_filter != "all"
        macro_count_label.set_text(
            t("Items: {visible} / {total}", visible=len(visible_macros), total=state.total_macro_rows)
            if filter_active
            else t("Items: {visible}", visible=len(visible_macros))
        )

        total_pages = max(1, (max(state.total_macro_rows, 1) + state.list_page_size - 1) // state.list_page_size)
        prev_page_button.set_enabled(state.list_page_index > 0)
        next_page_button.set_enabled((state.list_page_index + 1) < total_pages)

        if not visible_macros:
            with state.macro_list:
                ui.item(t("No macros indexed yet.") if not state.cached_macros else t("No matches."))
            state.viewer.set_macro(None, [])
            return

        selected_macro = selected_or_first_macro(visible_macros, state.selected_key)
        if selected_macro is None:
            state.viewer.set_macro(None, [])
            return
        state.selected_key = macro_key(selected_macro)

        _ver_key = f"{selected_macro['file_path']}::{selected_macro['macro_name']}"
        if _ver_key != state._cached_versions_key:
            state._cached_versions_key = _ver_key
            state._cached_versions = service.load_versions(
                str(selected_macro["file_path"]),
                str(selected_macro["macro_name"]),
            )
        versions = state._cached_versions

        def choose_macro(macro: dict[str, object]) -> None:
            state.selected_key = macro_key(macro)
            render_macro_list()

        with state.macro_list:
            for macro in visible_macros:
                button_classes = "flex-1 justify-start normal-case text-left items-start"
                file_label_classes = "text-[11px]"
                if macro_key(macro) == state.selected_key:
                    button_classes += " bg-blue-9 text-white"
                    file_label_classes += " text-blue-1"
                else:
                    file_label_classes += " text-grey-5"
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    file_name = Path(str(macro["file_path"])).name
                    is_deleted = bool(macro.get("is_deleted", False))
                    vc = _to_int(macro.get("version_count", 1), default=1)
                    with ui.button(on_click=lambda _event, m=macro: choose_macro(m)).props(
                        "flat no-caps align=left"
                    ).classes(button_classes):
                        with ui.row().classes("items-center gap-1.5 no-wrap"):
                            ui.label(str(vc)).classes(
                                "text-[10px] font-mono text-grey-5 bg-grey-8 rounded px-1 py-0.5 shrink-0 leading-none"
                            )
                            with ui.column().classes("items-start gap-0"):
                                name_classes = "text-left leading-tight"
                                if is_deleted:
                                    name_classes += " text-grey-5"
                                ui.label(str(macro.get("display_name") or macro.get("macro_name", ""))).classes(name_classes)
                                ui.label(f"({file_name})").classes(file_label_classes + " leading-tight")
                    render_status_badge(status_badge_key(macro))

        active_macro = find_active_override(selected_macro, state.cached_macros)

        selected_macro_key = macro_key(selected_macro)
        prefer_latest = state.force_latest_for_key == selected_macro_key
        prefer_active = state.force_active_for_key == selected_macro_key
        if prefer_latest:
            state.force_latest_for_key = None
        if prefer_active:
            state.force_active_for_key = None

        state.viewer.set_macro(
            selected_macro,
            versions,
            active_macro=active_macro,
            prefer_latest=prefer_latest,
            prefer_active=prefer_active,
        )
        state.viewer.set_editing_enabled(_remote_actions_available())

    def render_backup_list() -> None:
        """Render right-panel backup entries and attach action handlers."""
        backup_list.clear()
        backups = service.list_backups()
        if not backups:
            with backup_list:
                ui.item(t("No backups created yet."))
            return

        def open_backup_contents(backup: dict[str, object]) -> None:
            """Open contents dialog for one backup snapshot."""
            backup_id = _to_int(backup.get("backup_id", 0))
            items = service.load_backup_contents(backup_id)
            backup_view_title.set_text(t("Backup: {backup_name}", backup_name=backup.get("backup_name", "-")))
            backup_view_subtitle.set_text(
                t(
                    "Created {created_at} - {macro_count} macro(s)",
                    created_at=_format_ts(_to_int(backup.get("created_at", 0))),
                    macro_count=len(items),
                )
            )

            backup_view_table.rows = [
                {
                    "_row_id": f"{item['file_path']}::{item['macro_name']}",
                    "macro_name": item["macro_name"],
                    "file_path": item["file_path"],
                    "version": item["version"],
                    "status": t("active") if item.get("is_active", False) else t("inactive"),
                }
                for item in items
            ]
            backup_view_table.update()
            state.backup_view_dialog.open()

        def open_restore_dialog(backup: dict[str, object]) -> None:
            """Prepare and open restore confirmation dialog for one backup."""
            state.restore_target_id = _to_int(backup.get("backup_id", 0))
            state.restore_target_name = str(backup.get("backup_name", "-")).strip() or "-"
            state.restore_error_label.set_text("")
            state.restore_confirm_label.set_text(
                t(
                    "Restore backup '{backup_name}'? This replaces the current indexed macro state.",
                    backup_name=state.restore_target_name,
                )
            )
            state.restore_dialog.open()

        def open_delete_dialog(backup: dict[str, object]) -> None:
            """Prepare and open delete confirmation dialog for one backup."""
            state.delete_target_id = _to_int(backup.get("backup_id", 0))
            state.delete_target_name = str(backup.get("backup_name", "-")).strip() or "-"
            state.delete_error_label.set_text("")
            state.delete_confirm_label.set_text(
                t("Delete backup '{backup_name}'? This cannot be undone.", backup_name=state.delete_target_name)
            )
            state.delete_dialog.open()

        with backup_list:
            for backup in backups:
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    ui.label(str(backup.get("backup_name", "-")).strip() or "-").classes(
                        "flex-1 text-sm"
                    )
                    ui.label(_format_ts(_to_int(backup.get("created_at", 0)))).classes(
                        "text-[11px] text-grey-5"
                    )
                    ui.button(icon="search", on_click=lambda _event, b=backup: open_backup_contents(b)).props(
                        "flat dense round"
                    ).classes("text-blue-5")
                    ui.button(icon="restore", on_click=lambda _event, b=backup: open_restore_dialog(b)).props(
                        "flat dense round"
                    ).classes("text-orange-6")
                    ui.button(icon="delete", on_click=lambda _event, b=backup: open_delete_dialog(b)).props(
                        "flat dense round"
                    ).classes("text-red-6")

    def perform_restore() -> None:
        """Restore backup to DB and cfg files, then rescan to reflect on-disk state."""
        if state.restore_target_id is None:
            state.restore_error_label.set_text(t("No backup selected."))
            return

        try:
            result = service.restore_backup(state.restore_target_id)
        except Exception as exc:
            if _show_remote_conflict_guidance(
                operation_label=t("backup restore"),
                error=exc,
                local_error_label=state.restore_error_label,
            ):
                return
            state.restore_error_label.set_text(t("Restore failed: {error}", error=exc))
            state.status_label.set_text(t("Restore failed: {error}", error=exc))
            return

        state.restore_dialog.close()
        restored_label = _format_ts(_to_int(result.get("restored_at", 0)))
        rewritten = _to_int(result.get("restored_cfg_files", 0))
        if rewritten > 0:
            state.status_label.set_text(
                t(
                    "Restored backup '{backup_name}' at {restored_at} with {macro_count} macro(s); rewrote {cfg_file_count} cfg file(s). Local changes pending; click Save Config to upload.",
                    backup_name=result["backup_name"],
                    restored_at=restored_label,
                    macro_count=result["macro_count"],
                    cfg_file_count=rewritten,
                )
            )
        else:
            state.status_label.set_text(
                t(
                    "Restored backup '{backup_name}' at {restored_at} with {macro_count} macro(s). "
                    "No cfg snapshot was stored in this backup; only DB state was restored. Local changes pending; click Save Config to upload.",
                    backup_name=result["backup_name"],
                    restored_at=restored_label,
                    macro_count=result["macro_count"],
                )
            )
        _mark_local_changes_pending()
        _mark_reload_required(is_dynamic=False)
        perform_index("backup restore", sync_remote=False)

    def perform_delete_backup() -> None:
        """Delete selected backup and refresh the backup list."""
        if blocked_by_print_state(
            status_message="Blocked: printer is currently printing. Delete is disabled.",
            local_error_label=state.delete_error_label,
        ):
            return
        if state.delete_target_id is None:
            state.delete_error_label.set_text(t("No backup selected."))
            return

        try:
            result = service.delete_backup(state.delete_target_id)
        except Exception as exc:
            state.delete_error_label.set_text(t("Delete failed: {error}", error=exc))
            state.status_label.set_text(t("Delete failed: {error}", error=exc))
            return

        state.delete_dialog.close()
        state.status_label.set_text(t("Deleted backup '{backup_name}'.", backup_name=result["backup_name"]))
        render_backup_list()

    def refresh_data() -> None:
        """Reload all list/stats data from SQLite and rerender UI sections."""
        _note_activity()
        try:
            stats, state.cached_macros = service.load_dashboard(limit=state.list_page_size, offset=state.list_page_index * state.list_page_size)
        except Exception as exc:
            state.status_label.set_text(t("Data refresh failed: {error}", error=exc))
            return
        state.total_macro_rows = _to_int(stats.get("total_macros", len(state.cached_macros)), default=len(state.cached_macros))

        total_pages = max(1, (max(state.total_macro_rows, 1) + state.list_page_size - 1) // state.list_page_size)
        if state.list_page_index >= total_pages:
            state.list_page_index = max(0, total_pages - 1)
            try:
                stats, state.cached_macros = service.load_dashboard(limit=state.list_page_size, offset=state.list_page_index * state.list_page_size)
            except Exception as exc:
                state.status_label.set_text(t("Data refresh failed: {error}", error=exc))
                return
            state.total_macro_rows = _to_int(stats.get("total_macros", len(state.cached_macros)), default=len(state.cached_macros))

        deleted_macros = _to_int(stats.get("deleted_macros", 0))
        state.deleted_macro_count = deleted_macros
        state.cached_duplicate_names = duplicate_names_for_macros(state.cached_macros)
        duplicate_groups = service.list_duplicates()
        duplicate_macros = len(duplicate_groups)
        state._cached_versions_key = None
        state._cached_versions = []
        state.duplicate_warning_button.set_visibility(duplicate_macros > 0)
        total_macros_label.set_text(t("Total macros: {count}", count=stats["total_macros"]))
        duplicate_macros_label.set_text(t("Duplicate macros: {count}", count=duplicate_macros))
        deleted_macros_label.set_text(t("Deleted macros: {count}", count=deleted_macros))
        purge_deleted_button.set_visibility(deleted_macros > 0)
        distinct_files_label.set_text(t("Config files: {count}", count=stats["distinct_cfg_files"]))
        last_update_label.set_text(t("Last update: {value}", value=_format_ts(_to_optional_int(stats.get("latest_update_ts")))))

        render_macro_list()
        render_backup_list()

    def perform_index(trigger: str, *, sync_remote: bool = True) -> None:
        """Run cfg indexing and refresh UI when complete."""
        if state.is_indexing:
            return
        if off_printer_mode_enabled:
            refresh_off_printer_profile_state()
        if off_printer_mode_enabled and not state.off_printer_profile_ready:
            message = t("Cannot scan macros: configure and activate a printer connection first.")
            state.status_label.set_text(message)
            ui.notify(message, type="warning")
            return
        _note_activity()
        state.is_indexing = True
        try:
            state.status_label.set_text(t("Scanning macros ({trigger})...", trigger=trigger))
            result = _run_with_file_operation_modal(
                t("Scanning and parsing cfg files"),
                lambda: service.index(progress_callback=_set_file_operation_progress, sync_remote=sync_remote),
            )
            status_text = t(
                "Stored {inserted} new version(s), {unchanged} unchanged - {scanned} .cfg files scanned",
                inserted=result["macros_inserted"],
                unchanged=result["macros_unchanged"],
                scanned=result["cfg_files_scanned"],
            )
            remote_sync = result.get("remote_sync") if isinstance(result, dict) else None
            if isinstance(remote_sync, dict):
                synced_files = _to_int(remote_sync.get("synced_files", 0), default=0)
                removed_files = _to_int(remote_sync.get("removed_local_files", 0), default=0)
                status_text = (
                    f"{status_text} | "
                    + t("Remote sync: {synced} fetched, {removed} removed", synced=synced_files, removed=removed_files)
                )
            state.status_label.set_text(status_text)
            if trigger != "startup" and _to_int(result.get("macros_inserted", 0)) > 0:
                inserted = _to_int(result.get("macros_inserted", 0))
                dynamic_inserted = _to_int(result.get("dynamic_macros_inserted", 0))
                _mark_reload_required(is_dynamic=(inserted > 0 and dynamic_inserted == inserted))
            refresh_data()
            active_profile = service.get_active_printer_profile()
            runtime_config_dir_raw = str(result.get("runtime_config_dir", "") or "").strip()
            if not runtime_config_dir_raw:
                if hasattr(service, "get_runtime_config_dir"):
                    runtime_config_dir_raw = str(service.get_runtime_config_dir())
                else:
                    runtime_config_dir_raw = str(config_dir)
            if isinstance(active_profile, dict):
                vendor = str(active_profile.get("vendor", "")).strip()
                model = str(active_profile.get("model", "")).strip()
                if not vendor or not model:
                    state.printer_vendor_input.set_value(vendor)
                    state.printer_model_input.set_value(model)
                    state.printer_profile_dialog.open()
        except FileNotFoundError as exc:
            state.status_label.set_text(t("Error: {error}", error=exc))
        except Exception as exc:
            state.status_label.set_text(t("Scan failed: {error}", error=exc))
        finally:
            state.is_indexing = False

    def _maybe_run_deferred_startup_scan(reason: str) -> None:
        """Run one deferred startup scan once off-printer prerequisites are ready."""
        if not state.deferred_startup_scan:
            return
        if state.is_indexing or state.printer_is_printing:
            return
        if off_printer_mode_enabled and not state.off_printer_profile_ready:
            return

        state.deferred_startup_scan = False
        perform_index("startup")

    def open_backup_dialog() -> None:
        """Open backup creation dialog with generated default name."""
        backup_name_input.value = datetime.now().strftime("backup-%Y%m%d-%H%M%S")
        backup_name_input.update()
        state.backup_dialog.open()

    def open_load_order_overview_dialog() -> None:
        """Open a simple overview of cfg and macro parsing order for Klipper."""
        try:
            overview = service.load_cfg_loading_overview()
        except Exception as exc:
            status_label.set_text(t("Failed to load cfg parsing overview: {error}", error=exc))
            return

        file_rows_raw = overview.get("klipper_order", [])
        file_rows = [row for row in file_rows_raw if isinstance(row, dict)] if isinstance(file_rows_raw, list) else []
        macro_rows_raw = overview.get("klipper_macro_order", [])
        macro_rows = [row for row in macro_rows_raw if isinstance(row, dict)] if isinstance(macro_rows_raw, list) else []

        load_order_summary_label.set_text(
            t(
                "Klipper parses {klipper_count} cfg file(s) and {klipper_macro_count} macro section(s).",
                klipper_count=overview.get("klipper_count", len(file_rows)),
                klipper_macro_count=overview.get("klipper_macro_count", len(macro_rows)),
            )
        )

        lines = [t("Files"), "=" * 80]
        for row in file_rows:
            lines.append(f"{int(row.get('order', 0)):>4}  {str(row.get('file_path', ''))}")

        lines.extend(["", t("Macros"), "=" * 80])
        for row in macro_rows:
            lines.append(
                f"{int(row.get('order', 0)):>4}  "
                f"{str(row.get('macro_name', ''))}  "
                f"[{str(row.get('file_path', ''))}:{int(row.get('line_number', 0))}]"
            )

        load_order_text.set_text("\n".join(lines))
        state.load_order_dialog.open()

    def perform_backup() -> None:
        """Create named backup snapshot and update status/list output."""
        if state.printer_is_printing:
            backup_error_label.set_text(t("Blocked while printer is printing."))
            status_label.set_text(t("Blocked: printer is currently printing. Backup is disabled."))
            return
        name = str(backup_name_input.value or "").strip()
        if not name:
            backup_error_label.set_text(t("Please enter a backup name."))
            return

        try:
            result = service.create_backup(name)
        except Exception as exc:
            backup_error_label.set_text(t("Backup failed: {error}", error=exc))
            status_label.set_text(t("Backup failed: {error}", error=exc))
            return

        state.backup_dialog.close()
        created_label = _format_ts(_to_int(result.get("created_at", 0)))
        status_label.set_text(
            t(
                "Backup '{backup_name}' created at {created_at} with {macro_count} macro(s) from {cfg_file_count} cfg file(s).",
                backup_name=result["backup_name"],
                created_at=created_label,
                macro_count=result["macro_count"],
                cfg_file_count=result["cfg_file_count"],
            )
        )
        render_backup_list()

    def open_export_dialog() -> None:
        """Open macro export dialog with selectable latest macro identities."""
        export_macro_checkboxes.clear()
        state.export_macro_list.clear()
        with state.export_macro_list:
            for macro in state.cached_macros:
                identity = f"{str(macro.get('file_path', ''))}::{str(macro.get('macro_name', ''))}"
                label = f"{str(macro.get('display_name') or macro.get('macro_name', ''))} ({str(macro.get('file_path', ''))})"
                checkbox = ui.checkbox(label, value=(identity == state.selected_key)).props("dense")
                export_macro_checkboxes[identity] = checkbox
        export_error_label.set_text("")
        state.export_dialog.open()

    def perform_export() -> None:
        """Export selected macros to a share file on disk."""
        source_vendor, source_model = _active_printer_identity()
        selections = [
            identity
            for identity, checkbox in export_macro_checkboxes.items()
            if bool(getattr(checkbox, "value", False))
        ]
        if not selections:
            export_error_label.set_text(t("Select at least one macro to export."))
            return

        identities: list[tuple[str, str]] = []
        for identity in selections:
            if "::" not in identity:
                continue
            file_path, macro_name = identity.split("::", 1)
            identities.append((file_path, macro_name))

        if not identities:
            export_error_label.set_text(t("Select at least one macro to export."))
            return

        generated_name = datetime.now().strftime("klippervault-macros-share-%Y%m%d-%H%M%S.json")
        out_path = Path(tempfile.gettempdir()) / generated_name

        try:
            result = service.export_macro_share_file(
                identities=identities,
                source_vendor=source_vendor,
                source_model=source_model,
                out_file=out_path,
            )
        except Exception as exc:
            export_error_label.set_text(t("Export failed: {error}", error=exc))
            status_label.set_text(t("Export failed: {error}", error=exc))
            return

        state.export_dialog.close()
        exported_path = Path(str(result.get("file_path", "")))
        ui.download(exported_path, filename=exported_path.name)
        status_label.set_text(
            t(
                "Exported {count} macro(s) to {path}.",
                count=result.get("macro_count", 0),
                path=result.get("file_path", ""),
            )
        )

    def open_import_dialog() -> None:
        """Open macro import dialog."""
        state.uploaded_import_bytes = None
        state.uploaded_import_name = ""
        state.import_uploader.reset()
        state.import_error_label.set_text("")
        state.import_dialog.open()

    def perform_import() -> None:
        """Import a macro share file and refresh dashboard state."""
        target_vendor, target_model = _active_printer_identity()
        if not state.uploaded_import_bytes:
            state.import_error_label.set_text(t("Please upload a macro share file."))
            return

        suffix = Path(state.uploaded_import_name).suffix or ".json"
        temp_import_file = Path(tempfile.gettempdir()) / (
            datetime.now().strftime("klippervault-import-%Y%m%d-%H%M%S") + suffix
        )
        temp_import_file.write_bytes(state.uploaded_import_bytes)

        try:
            result = service.import_macro_share_file(
                import_file=temp_import_file,
                target_vendor=target_vendor,
                target_model=target_model,
            )
        except Exception as exc:
            state.import_error_label.set_text(t("Import failed: {error}", error=exc))
            state.status_label.set_text(t("Import failed: {error}", error=exc))
            return
        finally:
            temp_import_file.unlink(missing_ok=True)

        state.import_dialog.close()
        imported = _to_int(result.get("imported", 0))
        source_vendor = str(result.get("source_vendor", "")).strip()
        source_model = str(result.get("source_model", "")).strip()
        if imported <= 0:
            state.status_label.set_text(t("No macros were imported."))
            return

        if bool(result.get("printer_matches", False)):
            state.status_label.set_text(t("Imported {count} macro(s) as new inactive entries.", count=imported))
        elif source_vendor and source_model:
            state.status_label.set_text(
                t(
                    "Imported {count} macro(s) for printer {vendor} {model}. Review before enabling.",
                    count=imported,
                    vendor=source_vendor,
                    model=source_model,
                )
            )
        else:
            state.status_label.set_text(
                t(
                    "Imported {count} macro(s) with unknown source printer. Review before enabling.",
                    count=imported,
                )
            )

        refresh_data()

    def export_online_update_repo_zip() -> None:
        """Export active local macros as a ZIP for the online update repository."""
        if _printer_profile_missing():
            status_label.set_text(t("Set printer vendor/model before exporting update repository zip."))
            return
        source_vendor, source_model = _active_printer_identity()

        generated_name = datetime.now().strftime("klippervault-online-update-repo-%Y%m%d-%H%M%S.zip")
        out_path = Path(tempfile.gettempdir()) / generated_name

        try:
            result = service.export_online_update_repository_zip(
                out_file=out_path,
                source_vendor=source_vendor,
                source_model=source_model,
                repo_url=vault_cfg.online_update_repo_url,
                repo_ref=vault_cfg.online_update_ref,
                manifest_path=vault_cfg.online_update_manifest_path,
            )
        except Exception as exc:
            status_label.set_text(t("Update repository export failed: {error}", error=exc))
            return

        exported_path = Path(str(result.get("file_path", "")))
        ui.download(exported_path, filename=exported_path.name)
        status_label.set_text(
            t(
                "Exported {count} active macro(s) as update repository ZIP: {path}",
                count=result.get("macro_count", 0),
                path=result.get("file_path", ""),
            )
        )

    def _default_pr_head_branch() -> str:
        """Build a unique default branch name for PR publishing."""
        source_vendor, source_model = _active_printer_identity()
        vendor = source_vendor.lower().replace(" ", "-") or "printer"
        model = source_model.lower().replace(" ", "-") or "model"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return f"klippervault/{vendor}-{model}/{stamp}"

    def _refresh_create_pr_progress_ui() -> None:
        """Sync pull-request progress widgets with current background state."""
        if not state.create_pr_in_progress:
            create_pr_progress_label.set_visibility(False)
            create_pr_progress_bar.set_visibility(False)
            return

        display_total = max(state.create_pr_progress_total, 1)
        progress_value = min(max(state.create_pr_progress_current / display_total, 0.0), 1.0)
        percent = int(round(progress_value * 100.0))
        create_pr_progress_label.set_text(
            t(
                "Creating pull request: {percent}%",
                percent=percent,
            )
        )
        create_pr_progress_bar.value = progress_value
        create_pr_progress_bar.update()
        create_pr_progress_label.set_visibility(True)
        create_pr_progress_bar.set_visibility(True)

    def open_create_pr_dialog() -> None:
        """Open pull request creation dialog with convenience defaults."""
        source_vendor, source_model = _active_printer_identity()

        state.pr_repo_url_input.set_value(str(vault_cfg.online_update_repo_url or "").strip())
        state.pr_base_branch_input.set_value(str(vault_cfg.online_update_ref or "main").strip() or "main")
        state.pr_head_branch_input.set_value(_default_pr_head_branch())
        state.pr_title_input.set_value(
            t(
                "Update macros for {vendor} {model}",
                vendor=source_vendor or "printer",
                model=source_model or "model",
            )
        )
        state.pr_body_input.set_value(
            t(
                "Automated KlipperVault update for {vendor} {model}.",
                vendor=source_vendor or "printer",
                model=source_model or "model",
            )
        )
        state.pr_token_input.set_value("")
        state.create_pr_error_label.set_text("")
        state.create_pr_in_progress = False
        state.create_pr_progress_current = 0
        state.create_pr_progress_total = 1
        _refresh_create_pr_progress_ui()
        state.confirm_create_pr_button.set_enabled(True)
        state.create_pr_dialog.open()

    async def perform_create_pr() -> None:
        """Create a pull request on GitHub for current active macro artifacts."""
        source_vendor, source_model = _active_printer_identity()

        if _printer_profile_missing():
            state.create_pr_error_label.set_text(t("Set printer vendor/model before creating a pull request."))
            return

        repo_url = str(state.pr_repo_url_input.value or "").strip()
        base_branch = str(state.pr_base_branch_input.value or "").strip()
        head_branch = str(state.pr_head_branch_input.value or "").strip()
        title = str(state.pr_title_input.value or "").strip()
        body = str(state.pr_body_input.value or "").strip()
        token = str(state.pr_token_input.value or "").strip()

        if not repo_url or not base_branch or not head_branch or not title or not token:
            state.create_pr_error_label.set_text(t("Repository URL, branches, title, and token are required."))
            return

        state.create_pr_in_progress = True
        state.create_pr_progress_current = 0
        state.create_pr_progress_total = 1
        state.confirm_create_pr_button.set_enabled(False)
        state.create_pr_error_label.set_text("")
        state.status_label.set_text(t("Creating GitHub pull request..."))
        _refresh_create_pr_progress_ui()
        await asyncio.sleep(0)

        def report_progress(current: int, total: int) -> None:
            state.create_pr_progress_current = max(int(current), 0)
            state.create_pr_progress_total = max(int(total), 1)

        try:
            result = await asyncio.to_thread(
                service.create_online_update_pull_request,
                source_vendor=source_vendor,
                source_model=source_model,
                repo_url=repo_url,
                base_branch=base_branch,
                head_branch=head_branch,
                manifest_path=vault_cfg.online_update_manifest_path,
                github_token=token,
                pull_request_title=title,
                pull_request_body=body,
                progress_callback=report_progress,
            )
        except Exception as exc:
            state.create_pr_in_progress = False
            _refresh_create_pr_progress_ui()
            state.create_pr_error_label.set_text(t("Create PR failed: {error}", error=exc))
            state.status_label.set_text(t("Create PR failed: {error}", error=exc))
            state.confirm_create_pr_button.set_enabled(True)
            return

        state.create_pr_in_progress = False
        _refresh_create_pr_progress_ui()
        state.confirm_create_pr_button.set_enabled(True)
        state.create_pr_dialog.close()
        pr_number = _to_int(result.get("pull_request_number", 0))
        pr_url = str(result.get("pull_request_url", "")).strip()
        updated_files = _to_int(result.get("updated_files", 0))
        macro_count = _to_int(result.get("macro_count", 0))
        commit_count = _to_int(result.get("commit_count", 0))

        if bool(result.get("no_changes", False)):
            message = t("No macro changes detected for pull request. PR was not created.")
            state.status_label.set_text(message)
            ui.notify(message, type="warning")
            return

        if bool(result.get("existing", False)):
            state.status_label.set_text(
                t(
                    "Open pull request already exists (#{number}): {url}",
                    number=pr_number,
                    url=pr_url or "-",
                )
            )
            return

        state.status_label.set_text(
            t(
                "Created pull request #{number} with {files} updated file(s), {commits} commit(s), for {count} macro(s): {url}",
                number=pr_number,
                files=updated_files,
                commits=commit_count,
                count=macro_count,
                url=pr_url or "-",
            )
        )

    def _refresh_online_update_progress_ui() -> None:
        """Sync online update progress widgets with current background state."""
        if not state.online_update_check_in_progress:
            online_update_progress_label.set_visibility(False)
            online_update_progress_bar.set_visibility(False)
            return

        display_total = max(state.online_update_progress_total, 1)
        progress_value = min(max(state.online_update_progress_current / display_total, 0.0), 1.0)
        percent = int(round(progress_value * 100.0))
        online_update_progress_label.set_text(
            t(
                "Checking updates: {percent}%",
                percent=percent,
            )
        )
        online_update_progress_bar.value = progress_value
        online_update_progress_bar.update()
        online_update_progress_label.set_visibility(True)
        online_update_progress_bar.set_visibility(True)

    async def open_online_update_dialog() -> None:
        """Check online source for changed macros and open update selection dialog."""
        source_vendor, source_model = _active_printer_identity()

        state.online_update_activate_checkboxes.clear()
        state.online_update_list.clear()
        state.online_update_error_label.set_text("")
        state.online_update_summary_label.set_text("")
        state.pending_online_updates = []
        state.confirm_online_update_button.set_enabled(False)
        state.confirm_online_update_button.set_visibility(False)

        if _printer_profile_missing():
            state.status_label.set_text(t("Set printer vendor/model before checking updates."))
            return

        repo_url = str(vault_cfg.online_update_repo_url or "").strip()
        if not repo_url:
            state.status_label.set_text(t("Online updater repository URL is not configured."))
            return

        state.online_update_check_in_progress = True
        state.online_update_progress_current = 0
        state.online_update_progress_total = 1
        state.online_update_summary_label.set_text(t("Checking for updates..."))
        _refresh_online_update_progress_ui()
        state.online_update_dialog.open()
        await asyncio.sleep(0)

        def report_progress(current: int, total: int) -> None:
            state.online_update_progress_current = max(int(current), 0)
            state.online_update_progress_total = max(int(total), 0)

        try:
            result = await asyncio.to_thread(
                service.check_online_updates,
                repo_url=repo_url,
                manifest_path=vault_cfg.online_update_manifest_path,
                repo_ref=vault_cfg.online_update_ref,
                source_vendor=source_vendor,
                source_model=source_model,
                progress_callback=report_progress,
            )
        except Exception as exc:
            state.online_update_check_in_progress = False
            _refresh_online_update_progress_ui()
            state.status_label.set_text(t("Update check failed: {error}", error=exc))
            return

        state.online_update_check_in_progress = False
        _refresh_online_update_progress_ui()

        updates = result.get("updates", [])
        state.pending_online_updates = [item for item in updates if isinstance(item, dict)] if isinstance(updates, list) else []
        checked = _to_int(result.get("checked", 0))
        changed = _to_int(result.get("changed", 0))
        unchanged = _to_int(result.get("unchanged", 0))
        state.online_update_summary_label.set_text(
            t(
                "Checked {checked} macro(s): {changed} update(s), {unchanged} unchanged.",
                checked=checked,
                changed=changed,
                unchanged=unchanged,
            )
        )

        with state.online_update_list:
            if not state.pending_online_updates:
                ui.label(t("No online updates available.")).classes("text-sm text-grey-5")
            for item in state.pending_online_updates:
                identity = str(item.get("identity", ""))
                macro_name = str(item.get("macro_name", "")).strip() or t("Unnamed macro")
                local_version = _to_int(item.get("local_version", 0))
                remote_version = str(item.get("remote_version", "")).strip()
                version_label = t("local v{local} -> remote {remote}", local=local_version, remote=remote_version or "-")
                row_label = f"{macro_name} ({version_label})"
                checkbox = ui.checkbox(row_label, value=False).props("dense")
                state.online_update_activate_checkboxes[identity] = checkbox

            state.confirm_online_update_button.set_enabled(bool(state.pending_online_updates))
            state.confirm_online_update_button.set_visibility(bool(state.pending_online_updates))
            state.online_update_dialog.open()
        state.status_label.set_text(t("Online update check complete."))

    async def _check_online_updates_on_startup() -> None:
        """Run one background online update check on every app startup when repository is configured."""
        source_vendor, source_model = _active_printer_identity()

        if state.startup_online_update_check_in_progress:
            return
        if not source_vendor or not source_model:
            return

        repo_url = str(vault_cfg.online_update_repo_url or "").strip()
        if not repo_url:
            return

        state.startup_online_update_check_in_progress = True
        try:
            result = await asyncio.to_thread(
                service.check_online_updates,
                repo_url=repo_url,
                manifest_path=vault_cfg.online_update_manifest_path,
                repo_ref=vault_cfg.online_update_ref,
                source_vendor=source_vendor,
                source_model=source_model,
            )
        except Exception:
            state.startup_online_update_check_in_progress = False
            return

        state.startup_online_update_check_in_progress = False

        changed = _to_int(result.get("changed", 0))
        if changed <= 0:
            return

        checked = _to_int(result.get("checked", 0))
        message = t(
            "Startup update check found {changed} update(s) out of {checked} macro(s).",
            changed=changed,
            checked=checked,
        )
        state.status_label.set_text(message)
        # This coroutine runs via asyncio.create_task from a timer callback.
        # Avoid ui.notify here because it requires an active NiceGUI slot/client context.
        try:
            await asyncio.to_thread(service.send_mainsail_notification, message=message)
        except Exception as exc:
            # Keep UI flow resilient if Moonraker notification delivery fails.
            state.status_label.set_text(t("Mainsail notification failed: {error}", error=exc))

    def perform_online_update_import() -> None:
        """Import checked online updates and activate only selected macros."""
        if not state.pending_online_updates:
            online_update_error_label.set_text(t("No online updates to import."))
            return

        activate_identities = [
            identity
            for identity, checkbox in state.online_update_activate_checkboxes.items()
            if bool(getattr(checkbox, "value", False))
        ]

        try:
            result = service.import_online_updates(
                updates=state.pending_online_updates,
                activate_identities=activate_identities,
                repo_url=vault_cfg.online_update_repo_url,
                repo_ref=vault_cfg.online_update_ref,
            )
        except Exception as exc:
            online_update_error_label.set_text(t("Import updates failed: {error}", error=exc))
            status_label.set_text(t("Import updates failed: {error}", error=exc))
            return

        imported = _to_int(result.get("imported", 0))
        activated = _to_int(result.get("activated", 0))
        if imported <= 0:
            online_update_error_label.set_text(t("No online updates were imported."))
            status_label.set_text(t("No online updates were imported."))
            return

        online_update_dialog.close()
        status_label.set_text(
            t(
                "Imported {imported} online update(s); activated {activated}.",
                imported=imported,
                activated=activated,
            )
        )
        if activated > 0:
            _mark_reload_required(is_dynamic=False)
            _mark_local_changes_pending()

        # Re-index immediately only when every imported update was activated.
        # Otherwise keep imported-but-not-activated rows visible as inactive.
        if activated > 0 and activated == imported:
            perform_index("online updates", sync_remote=False)
        else:
            refresh_data()

    def purge_deleted_macros() -> None:
        """Remove all deleted macro histories from SQLite in one action."""
        if state.printer_is_printing:
            status_label.set_text(t("Blocked: printer is currently printing. Purge is disabled."))
            return
        try:
            result = service.purge_all_deleted()
        except Exception as exc:
            status_label.set_text(t("Failed to purge deleted macros: {error}", error=exc))
            return

        removed = _to_int(result.get("removed", 0))
        if removed > 0:
            status_label.set_text(t("Purged {removed} deleted macro row(s) from database.", removed=removed))
        else:
            status_label.set_text(t("No deleted macros to purge."))
        refresh_data()

    def run_index() -> None:
        """Manual scan button handler."""
        perform_index("manual")

    def restart_klipper() -> None:
        """Request Klipper restart when macro changes are pending and printer is idle."""
        if not state.restart_required:
            status_label.set_text(t("No pending macro changes require a Klipper restart."))
            return
        if state.printer_is_printing or state.printer_is_busy:
            status_label.set_text(t("Blocked: printer is busy or printing. Klipper restart is disabled."))
            return

        try:
            service.restart_klipper(timeout=3.0)
        except Exception as exc:
            status_label.set_text(t("Failed to restart Klipper: {error}", error=exc))
            return

        _clear_restart_required()
        status_label.set_text(t("Klipper restart requested. The restart button will reappear after another macro change."))

    def reload_dynamic_macros() -> None:
        """Request dynamic macro reload when pending dynamic macro changes exist."""
        if not state.dynamic_reload_required:
            status_label.set_text(t("No pending dynamic macro changes require a dynamic macro reload."))
            return

        try:
            service.reload_dynamic_macros(timeout=3.0)
        except Exception as exc:
            status_label.set_text(t("Failed to reload dynamic macros: {error}", error=exc))
            return

        _clear_restart_required()
        status_label.set_text(
            t("Dynamic macro reload requested. The reload button will reappear after another dynamic macro change.")
        )

    def save_config_to_printer() -> None:
        """Explicitly upload local cfg changes to printer via SFTP when printer is idle."""
        if not off_printer_mode_enabled:
            status_label.set_text(t("Save Config is only available in off-printer mode."))
            return
        if not state.off_printer_profile_ready:
            status_label.set_text(t("Cannot save config: configure and activate a printer connection first."))
            return
        if state.printer_is_printing:
            status_label.set_text(t("Blocked: printer is currently printing. Save Config is disabled."))
            _refresh_save_config_button()
            return
        if not state.has_unsynced_local_changes:
            status_label.set_text(t("No local config changes pending upload."))
            _refresh_save_config_button()
            return

        try:
            result = _run_with_file_operation_modal(
                t("Uploading local cfg files to printer"),
                lambda: service.save_config_to_remote(progress_callback=_set_file_operation_progress),
            )
        except Exception as exc:
            status_label.set_text(t("Save Config failed: {error}", error=exc))
            return

        uploaded = _to_int(result.get("uploaded_files", 0), default=0)
        removed = _to_int(result.get("removed_remote_files", 0), default=0)
        blocked = _to_int(result.get("blocked_files", 0), default=0)
        status_label.set_text(
            t(
                "Save Config complete: {uploaded} uploaded, {removed} removed, {blocked} blocked.",
                uploaded=uploaded,
                removed=removed,
                blocked=blocked,
            )
        )
        _mark_local_changes_saved()
        _append_restart_policy_from_result(result)

    def _append_restart_policy_from_result(result: dict[str, object]) -> None:
        """Apply restart/dynamic-reload markers from service result payload."""
        if bool(result.get("restart_required", False)):
            _mark_reload_required(is_dynamic=False)
            return
        if bool(result.get("dynamic_reload_required", False)):
            _mark_reload_required(is_dynamic=True)

    def set_print_lock(locked: bool, moonraker_state: str, moonraker_message: str) -> None:
        """Toggle UI mutation lock while printer is actively printing."""
        _prev_printer_state = state.printer_state
        state.printer_is_printing = locked
        state.printer_state = moonraker_state
        state.printer_status_message = str(moonraker_message or "")
        state.printer_is_busy = moonraker_state not in {"standby", "ready", "complete", "cancelled"}
        local_actions_enabled = _remote_actions_available()

        index_button.set_enabled(local_actions_enabled)
        macro_actions_button.set_enabled(local_actions_enabled)
        duplicate_warning_button.set_enabled(local_actions_enabled)
        purge_deleted_button.set_enabled(local_actions_enabled and state.deleted_macro_count > 0)
        create_backup_button.set_enabled(local_actions_enabled)
        confirm_export_button.set_enabled(local_actions_enabled)
        confirm_import_button.set_enabled(local_actions_enabled)
        confirm_create_pr_button.set_enabled(local_actions_enabled)
        confirm_online_update_button.set_enabled(local_actions_enabled and bool(state.pending_online_updates))
        confirm_restore_button.set_enabled(local_actions_enabled)
        confirm_delete_button.set_enabled(local_actions_enabled)
        duplicate_compare_button.set_enabled(local_actions_enabled)
        duplicate_prev_button.set_enabled(local_actions_enabled and state.duplicate_wizard_index > 0)
        duplicate_next_button.set_enabled(
            local_actions_enabled and state.duplicate_wizard_index < len(state.duplicate_wizard_groups) - 1
        )
        duplicate_apply_button.set_enabled(local_actions_enabled)
        if off_printer_mode_enabled:
            off_printer_manage_profiles_button.set_enabled(True)
            off_printer_test_button.set_enabled(True)
            off_printer_cfg_list_button.set_enabled(state.off_printer_profile_ready)
        state.viewer.set_editing_enabled(local_actions_enabled)
        _refresh_reload_buttons()
        _refresh_save_config_button()

        if off_printer_mode_enabled and state.off_printer_profile_ready and state.off_printer_profile_label is not None:
            detail = str(moonraker_message or "").strip()
            if moonraker_state == "unknown":
                offline_text = t("Printer offline")
                if detail:
                    offline_text = t("Printer offline: {detail}", detail=detail)
                state.off_printer_profile_label.classes(replace="text-xs text-negative")
                state.off_printer_profile_label.set_text(offline_text)
            else:
                state.off_printer_profile_label.classes(replace="text-xs text-positive")
                state.off_printer_profile_label.set_text(state.off_printer_profile_status_text)

        if locked:
            status_label.set_text(
                t(
                    "Printing in progress ({state}). Local edits are allowed; Save Config upload is disabled.",
                    state=moonraker_state,
                )
            )
        else:
            if off_printer_mode_enabled and not state.off_printer_profile_ready:
                status_label.set_text(t("Ready (waiting for active printer connection)."))
            elif moonraker_state == "unknown":
                status_label.set_text(t("Ready (Moonraker status unknown)."))
            else:
                status_label.set_text(t("Ready (printer state: {state}).", state=moonraker_state))
            _maybe_run_deferred_startup_scan("printer became idle")

        # Detect printer coming back online after being unreachable and auto-rescan.
        if moonraker_state != "unknown":
            if (
                _prev_printer_state == "unknown"
                and state.printer_seen_connected
                and not locked
                and not state.is_indexing
            ):
                perform_index("printer came online")
            state.printer_seen_connected = True

    def refresh_print_state() -> None:
        """Poll Moonraker printer state and apply UI lock policy."""
        nonlocal _print_state_refresh_inflight
        if off_printer_mode_enabled and not state.off_printer_profile_ready:
            _set_printer_connecting_modal(False)
            set_print_lock(
                locked=False,
                moonraker_state="unknown",
                moonraker_message=t("Off-printer mode active but no printer connection is ready."),
            )
            return

        if _print_state_refresh_inflight:
            return

        async def _refresh_print_state_async() -> None:
            """Run Moonraker status query off the UI path to keep GUI responsive."""
            nonlocal _print_state_refresh_inflight
            _print_state_refresh_inflight = True
            try:
                status = await asyncio.to_thread(service.query_printer_status, 1.5)
                _set_printer_connecting_modal(False)
            except Exception as exc:
                _set_printer_connecting_modal(True, str(exc))
                status = {
                    "is_printing": False,
                    "state": "unknown",
                    "message": str(exc),
                }
            finally:
                _print_state_refresh_inflight = False

            set_print_lock(
                locked=bool(status.get("is_printing", False)),
                moonraker_state=str(status.get("state", "unknown")),
                moonraker_message=str(status.get("message", "")),
            )

        try:
            asyncio.create_task(_refresh_print_state_async())
        except RuntimeError:
            # Fallback for contexts without a running loop (e.g., early startup paths).
            try:
                status = service.query_printer_status(timeout=1.5)
                _set_printer_connecting_modal(False)
            except Exception as exc:
                _set_printer_connecting_modal(True, str(exc))
                status = {
                    "is_printing": False,
                    "state": "unknown",
                    "message": str(exc),
                }
            set_print_lock(
                locked=bool(status.get("is_printing", False)),
                moonraker_state=str(status.get("state", "unknown")),
                moonraker_message=str(status.get("message", "")),
            )

    def test_off_printer_profile_connection() -> None:
        """Run active SSH profile connectivity test and report status."""
        if not off_printer_mode_enabled:
            return
        try:
            result = service.test_active_ssh_connection()
        except Exception as exc:
            message = t("SSH profile test failed: {error}", error=exc)
            status_label.set_text(message)
            ui.notify(message, type="negative")
            return

        if bool(result.get("ok", False)):
            elapsed_ms = _to_int(result.get("elapsed_ms", 0), default=0)
            profile_name = str(result.get("profile_name", "")).strip() or t("unnamed")
            message = t("SSH profile '{profile}' connected in {elapsed}ms.", profile=profile_name, elapsed=elapsed_ms)
            status_label.set_text(message)
            ui.notify(message, type="positive")
            refresh_off_printer_profile_state()
            return

        error_text = str(result.get("error", "")).strip() or t("unknown error")
        message = t("SSH profile test failed: {error}", error=error_text)
        status_label.set_text(message)
        ui.notify(message, type="warning")

    def _set_auth_mode_fields() -> None:
        """Update secret input label for currently selected auth mode."""
        auth_mode = str(ssh_profile_auth_mode_select.value or "key").strip().lower()
        if auth_mode == "password":
            ssh_profile_secret_input.props("type=password")
            ssh_profile_secret_mode_label.set_text(t("Secret input expects SSH password."))
            ssh_profile_secret_input.update()
            return
        ssh_profile_secret_input.props("type=text")
        ssh_profile_secret_mode_label.set_text(t("Secret input expects SSH key path."))
        ssh_profile_secret_input.update()

    def _refresh_ssh_profile_action_buttons() -> None:
        """Enable profile actions only when a saved profile is selected."""
        selected_option = str(ssh_profile_select.value or "").strip()
        has_selection = selected_option in state.ssh_profile_option_ids
        delete_ssh_profile_button.set_enabled(has_selection)
        activate_ssh_profile_button.set_enabled(has_selection)

    def _set_selected_profile_secret_state(profile: dict[str, object] | None) -> None:
        """Show whether selected profile currently has stored credentials."""
        if not isinstance(profile, dict) or not profile:
            ssh_profile_secret_state_label.set_text(t("Secret status: set credentials when saving profile."))
            ssh_profile_secret_state_label.classes(replace="text-xs text-grey-5")
            return

        auth_mode = str(profile.get("auth_mode", "key")).strip().lower() or "key"
        has_secret = bool(profile.get("has_secret", False))
        backend = str(profile.get("secret_backend", "")).strip()
        backend_suffix = f" ({backend})" if backend else ""
        if has_secret:
            ssh_profile_secret_state_label.set_text(t("Secret status: configured") + backend_suffix)
            ssh_profile_secret_state_label.classes(replace="text-xs text-positive")
            return

        secret_type_label = t("password") if auth_mode == "password" else t("key path")
        ssh_profile_secret_state_label.set_text(
            t("Secret status: missing {secret_type}; enter and save.", secret_type=secret_type_label)
        )
        ssh_profile_secret_state_label.classes(replace="text-xs text-warning")

    def reset_ssh_profile_form_for_new() -> None:
        """Reset dialog fields for creating a fresh SSH profile."""
        ssh_profile_select.set_value("")
        ssh_profile_name_input.set_value("")
        ssh_profile_host_input.set_value("")
        ssh_profile_port_input.set_value(22)
        ssh_profile_username_input.set_value("")
        ssh_profile_remote_dir_input.set_value("~/printer_data/config")
        ssh_profile_moonraker_url_input.set_value("http://127.0.0.1:7125")
        ssh_profile_auth_mode_select.set_value("key")
        ssh_profile_secret_input.set_value("")
        ssh_profile_active_toggle.set_value(True)
        ssh_profile_error_label.set_text("")
        ssh_profile_status_label.set_text(t("Enter details to create a new SSH profile."))
        _set_selected_profile_secret_state(None)
        _set_auth_mode_fields()
        _refresh_ssh_profile_action_buttons()

    def _format_moonraker_url_host(host: str) -> str:
        """Return a URL-safe host string for the Moonraker endpoint."""
        normalized_host = str(host or "").strip() or "127.0.0.1"
        if ":" in normalized_host and not normalized_host.startswith("["):
            return f"[{normalized_host}]"
        return normalized_host

    def _sync_moonraker_url_host(host: str) -> None:
        """Keep the Moonraker URL host aligned with the current SSH host field."""
        raw_url = str(ssh_profile_moonraker_url_input.value or "").strip()
        if not raw_url:
            return

        parsed_url = urlparse(raw_url)
        if parsed_url.scheme not in {"http", "https"}:
            return

        try:
            port_suffix = f":{parsed_url.port}" if parsed_url.port is not None else ""
        except ValueError:
            return

        userinfo = ""
        if parsed_url.username:
            userinfo = parsed_url.username
            if parsed_url.password:
                userinfo += f":{parsed_url.password}"
            userinfo += "@"

        rewritten_url = parsed_url._replace(netloc=f"{userinfo}{_format_moonraker_url_host(host)}{port_suffix}")
        ssh_profile_moonraker_url_input.set_value(urlunparse(rewritten_url))

    def _load_selected_ssh_profile() -> None:
        """Populate profile form from the selected saved profile."""
        selected_option = str(ssh_profile_select.value or "").strip()
        selected_id = state.ssh_profile_option_ids.get(selected_option, 0)
        profile = state.ssh_profiles_by_id.get(int(selected_id), {}) if selected_id > 0 else {}
        if not profile:
            _refresh_ssh_profile_action_buttons()
            return

        ssh_profile_name_input.set_value(str(profile.get("profile_name", "")))
        ssh_profile_host_input.set_value(str(profile.get("host", "")))
        ssh_profile_port_input.set_value(_to_int(profile.get("port"), default=22))
        ssh_profile_username_input.set_value(str(profile.get("username", "")))
        ssh_profile_remote_dir_input.set_value(str(profile.get("remote_config_dir", "")))
        ssh_profile_moonraker_url_input.set_value(str(profile.get("moonraker_url", "")))
        auth_mode = str(profile.get("auth_mode", "key")).strip().lower() or "key"
        if auth_mode not in {"key", "password"}:
            auth_mode = "key"
        ssh_profile_auth_mode_select.set_value(auth_mode)
        _set_auth_mode_fields()
        ssh_profile_active_toggle.set_value(bool(profile.get("is_active", False)))
        _set_selected_profile_secret_state(profile)
        _refresh_ssh_profile_action_buttons()

    def refresh_ssh_profiles_dialog() -> None:
        """Refresh saved SSH profiles and sync dialog controls."""
        ssh_profile_error_label.set_text("")
        ssh_profile_status_label.set_text("")
        try:
            profiles = service.list_ssh_profiles()
        except Exception as exc:
            ssh_profile_error_label.set_text(t("Failed to load SSH profiles: {error}", error=exc))
            return

        state.ssh_profile_option_ids.clear()
        state.ssh_profiles_by_id.clear()

        options: list[str] = []
        selected_value = ""
        for raw_profile in profiles:
            if not isinstance(raw_profile, dict):
                continue
            profile_id = _to_int(raw_profile.get("id"), default=0)
            if profile_id <= 0:
                continue
            state.ssh_profiles_by_id[profile_id] = raw_profile
            profile_name = str(raw_profile.get("profile_name", "")).strip() or t("unnamed")
            host = str(raw_profile.get("host", "")).strip() or "?"
            port = _to_int(raw_profile.get("port"), default=22)
            active_suffix = " *" if bool(raw_profile.get("is_active", False)) else ""
            option_label = f"{profile_name} ({host}:{port}){active_suffix}"
            options.append(option_label)
            state.ssh_profile_option_ids[option_label] = profile_id
            if bool(raw_profile.get("is_active", False)):
                selected_value = option_label

        if not selected_value and options:
            selected_value = options[0]
        ssh_profile_select.set_options(options, value=selected_value)
        if selected_value:
            _load_selected_ssh_profile()
        else:
            reset_ssh_profile_form_for_new()
        _refresh_ssh_profile_action_buttons()

    def save_ssh_profile_from_dialog() -> None:
        """Persist one SSH profile and optional credentials from form values."""
        if not off_printer_mode_enabled:
            return
        ssh_profile_error_label.set_text("")
        ssh_profile_status_label.set_text("")

        profile_name = str(ssh_profile_name_input.value or "").strip()
        host = str(ssh_profile_host_input.value or "").strip()
        username = str(ssh_profile_username_input.value or "").strip()
        remote_config_dir = str(ssh_profile_remote_dir_input.value or "").strip()
        moonraker_url = str(ssh_profile_moonraker_url_input.value or "").strip()
        auth_mode = str(ssh_profile_auth_mode_select.value or "key").strip().lower() or "key"
        port = _to_int(ssh_profile_port_input.value, default=22)
        secret_value = str(ssh_profile_secret_input.value or "").strip()

        if not profile_name:
            ssh_profile_error_label.set_text(t("Profile name is required."))
            return
        if not host:
            ssh_profile_error_label.set_text(t("Host is required."))
            return
        if not username:
            ssh_profile_error_label.set_text(t("Username is required."))
            return
        if not remote_config_dir:
            ssh_profile_error_label.set_text(t("Remote config directory is required."))
            return
        if port < 1 or port > 65535:
            ssh_profile_error_label.set_text(t("Port must be between 1 and 65535."))
            return
        if auth_mode not in {"key", "password"}:
            ssh_profile_error_label.set_text(t("Authentication mode must be key or password."))
            return

        parsed_url = urlparse(moonraker_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            ssh_profile_error_label.set_text(t("Moonraker URL must start with http:// or https:// and include a host."))
            return

        selected_option = str(ssh_profile_select.value or "").strip()
        selected_id = state.ssh_profile_option_ids.get(selected_option, 0)
        selected_profile = state.ssh_profiles_by_id.get(int(selected_id), {}) if selected_id > 0 else {}
        selected_has_secret = bool(selected_profile.get("has_secret", False)) if isinstance(selected_profile, dict) else False
        if not secret_value and not selected_has_secret:
            secret_type_label = t("password") if auth_mode == "password" else t("key path")
            ssh_profile_error_label.set_text(t("Enter SSH {secret_type} before saving.", secret_type=secret_type_label))
            return

        try:
            result = service.save_ssh_profile(
                profile_name=profile_name,
                host=host,
                port=port,
                username=username,
                remote_config_dir=remote_config_dir,
                moonraker_url=moonraker_url,
                auth_mode=auth_mode,
                is_active=bool(ssh_profile_active_toggle.value),
                secret_value=secret_value if secret_value else None,
            )
        except Exception as exc:
            ssh_profile_error_label.set_text(t("Failed to save SSH profile: {error}", error=exc))
            return

        profile_id = _to_int(result.get("profile_id"), default=0)
        backend = str(result.get("secret_backend", "")).strip()
        backend_text = backend if backend else t("unchanged")
        ssh_profile_status_label.set_text(
            t("Profile saved. Secret backend: {backend}", backend=backend_text)
        )
        ssh_profile_secret_input.set_value("")
        refresh_ssh_profiles_dialog()
        if profile_id > 0:
            for option_label, option_profile_id in state.ssh_profile_option_ids.items():
                if option_profile_id == profile_id:
                    ssh_profile_select.set_value(option_label)
                    break
        _load_selected_ssh_profile()
        refresh_printer_profile_selector()
        refresh_off_printer_profile_state()
        refresh_print_state()
        off_printer_profile_dialog.close()

    def activate_selected_ssh_profile() -> None:
        """Activate the profile selected in the management dialog."""
        selected_option = str(ssh_profile_select.value or "").strip()
        selected_id = state.ssh_profile_option_ids.get(selected_option, 0)
        if selected_id <= 0:
            ssh_profile_error_label.set_text(t("Select a profile to activate."))
            return

        ssh_profile_error_label.set_text("")
        ssh_profile_status_label.set_text("")
        try:
            result = service.activate_ssh_profile(selected_id)
        except Exception as exc:
            ssh_profile_error_label.set_text(t("Failed to activate SSH profile: {error}", error=exc))
            return
        if not bool(result.get("ok", False)):
            ssh_profile_error_label.set_text(t("Failed to activate SSH profile."))
            return

        profile = state.ssh_profiles_by_id.get(int(selected_id), {})
        profile_name = str(profile.get("profile_name", "")).strip() if isinstance(profile, dict) else ""
        service.ensure_printer_profile_for_ssh_profile(
            ssh_profile_id=int(selected_id),
            profile_name=profile_name or t("Printer"),
            activate=True,
        )

        ssh_profile_status_label.set_text(t("Active SSH profile updated."))
        refresh_ssh_profiles_dialog()
        refresh_printer_profile_selector()
        refresh_off_printer_profile_state()
        refresh_print_state()

    def delete_selected_ssh_profile() -> None:
        """Delete selected profile from profile storage."""
        selected_option = str(ssh_profile_select.value or "").strip()
        selected_id = state.ssh_profile_option_ids.get(selected_option, 0)
        if selected_id <= 0:
            ssh_profile_error_label.set_text(t("Select a profile to delete."))
            return

        ssh_profile_error_label.set_text("")
        ssh_profile_status_label.set_text("")
        try:
            result = service.delete_ssh_profile(selected_id)
        except Exception as exc:
            ssh_profile_error_label.set_text(t("Failed to delete SSH profile: {error}", error=exc))
            return
        if not bool(result.get("ok", False)):
            ssh_profile_error_label.set_text(t("Failed to delete SSH profile."))
            return

        ssh_profile_status_label.set_text(t("SSH profile deleted."))
        refresh_ssh_profiles_dialog()
        refresh_off_printer_profile_state()
        refresh_print_state()

    def open_off_printer_profile_dialog() -> None:
        """Open and initialize SSH profile management dialog."""
        if not off_printer_mode_enabled:
            return
        refresh_ssh_profiles_dialog()
        _set_auth_mode_fields()
        off_printer_profile_dialog.open()

    def open_remote_cfg_list_dialog() -> None:
        """Load and display remote cfg file list for active SSH profile."""
        if not off_printer_mode_enabled:
            return

        remote_cfg_list_error.set_text("")
        remote_cfg_list_text.set_value("")
        try:
            result = service.list_active_remote_cfg_files()
        except Exception as exc:
            remote_cfg_list_subtitle.set_text(t("Failed to load remote cfg files."))
            remote_cfg_list_error.set_text(t("{error}", error=exc))
            ui.notify(t("Failed to load remote cfg files: {error}", error=exc), type="negative")
            remote_cfg_list_dialog.open()
            return

        profile_name = str(result.get("profile_name", "")).strip() or t("unnamed")
        count = _to_int(result.get("count", 0), default=0)
        remote_cfg_list_title.set_text(t("Remote cfg files"))
        remote_cfg_list_subtitle.set_text(
            t("Profile: {profile} | Files: {count}", profile=profile_name, count=count)
        )
        files = result.get("files", [])
        file_lines = []
        if isinstance(files, list):
            file_lines = [str(path) for path in files if str(path).strip()]
        remote_cfg_list_text.set_value("\n".join(file_lines))
        ui.notify(t("Loaded {count} remote cfg file(s).", count=count), type="info")
        remote_cfg_list_dialog.open()

    def check_config_changes() -> None:
        """Timer callback: auto-rescan when cfg files change."""
        refresh_print_state()
        if state.printer_is_printing:
            return
        if state.is_indexing:
            return

    def toggle_duplicates_filter() -> None:
        """Toggle duplicate-only filter and rerender list."""
        state.show_duplicates_only = not state.show_duplicates_only
        update_duplicates_button_label()
        render_macro_list()

    def _go_prev_page() -> None:
        """Navigate one macro-list page backward and refresh data."""
        if state.list_page_index <= 0:
            return
        state.list_page_index -= 1
        refresh_data()

    def _go_next_page() -> None:
        """Navigate one macro-list page forward and refresh data."""
        total_pages = max(1, (max(state.total_macro_rows, 1) + state.list_page_size - 1) // state.list_page_size)
        if (state.list_page_index + 1) >= total_pages:
            return
        state.list_page_index += 1
        refresh_data()

    def toggle_new_filter() -> None:
        """Toggle new-only filter and rerender list."""
        state.show_new_only = not state.show_new_only
        update_new_button_label()
        render_macro_list()

    def cycle_active_filter() -> None:
        """Cycle active filter through all -> active -> inactive."""
        if state.active_filter == "all":
            state.active_filter = "active"
        elif state.active_filter == "active":
            state.active_filter = "inactive"
        else:
            state.active_filter = "all"
        update_active_filter_button_label()
        render_macro_list()

    def on_search_change(e) -> None:
        """Search input change handler — updates query and marks list as dirty."""
        state.search_query = e.value or ""
        state._search_dirty = True

    update_duplicates_button_label()
    update_new_button_label()
    update_active_filter_button_label()
    sort_radio.on_value_change(on_sort_change)
    duplicate_keep_select.on_value_change(_on_duplicate_keep_change)
    duplicate_compare_with_select.on_value_change(_on_duplicate_compare_with_change)
    duplicate_compare_button.on_click(open_duplicate_pair_compare)
    duplicate_prev_button.on_click(duplicate_wizard_previous)
    duplicate_next_button.on_click(duplicate_wizard_next)
    duplicate_apply_button.on_click(apply_duplicate_resolution)
    duplicates_button.on_click(toggle_duplicates_filter)
    new_button.on_click(toggle_new_filter)
    active_filter_button.on_click(cycle_active_filter)
    state.macro_search.on_value_change(on_search_change)
    duplicate_warning_button.on_click(open_duplicate_wizard)
    off_printer_manage_profiles_button.on_click(open_off_printer_profile_dialog)
    off_printer_test_button.on_click(test_off_printer_profile_connection)
    off_printer_cfg_list_button.on_click(open_remote_cfg_list_dialog)
    ssh_profile_select.on_value_change(_load_selected_ssh_profile)
    ssh_profile_auth_mode_select.on_value_change(_set_auth_mode_fields)
    refresh_ssh_profiles_button.on_click(refresh_ssh_profiles_dialog)
    new_ssh_profile_button.on_click(reset_ssh_profile_form_for_new)
    delete_ssh_profile_button.on_click(delete_selected_ssh_profile)
    activate_ssh_profile_button.on_click(activate_selected_ssh_profile)
    save_ssh_profile_button.on_click(save_ssh_profile_from_dialog)
    active_printer_select.on_value_change(on_active_printer_profile_change)
    with macro_actions_menu:
        ui.menu_item(t("Backup"), on_click=open_backup_dialog)
        ui.menu_item(t("Export macros"), on_click=open_export_dialog)
        ui.menu_item(t("Import macros"), on_click=open_import_dialog)
        ui.menu_item(t("Loading order overview"), on_click=open_load_order_overview_dialog)
        ui.menu_item(t("Check for updates"), on_click=open_online_update_dialog)

    if developer_menu is not None:
        with developer_menu:
            ui.menu_item(t("Export Update Zip"), on_click=export_online_update_repo_zip)
            ui.menu_item(t("Create Pull Request"), on_click=open_create_pr_dialog)
    reload_dynamic_macros_button.on_click(reload_dynamic_macros)
    restart_klipper_button.on_click(restart_klipper)
    create_backup_button.on_click(perform_backup)
    confirm_export_button.on_click(perform_export)
    confirm_import_button.on_click(perform_import)
    confirm_create_pr_button.on_click(perform_create_pr)
    confirm_online_update_button.on_click(perform_online_update_import)
    purge_deleted_button.on_click(purge_deleted_macros)
    confirm_restore_button.on_click(perform_restore)
    confirm_delete_button.on_click(perform_delete_backup)
    confirm_macro_delete_button.on_click(confirm_macro_delete)

    index_button.on_click(run_index)
    save_config_button.on_click(save_config_to_printer)
    settings_toolbar_button.on_click(open_app_settings_dialog)
    prev_page_button.on_click(_go_prev_page)
    next_page_button.on_click(_go_next_page)

    refresh_printer_profile_selector()
    if off_printer_mode_enabled and _printer_profile_missing():
        status_label.set_text(t("No printer configured. Complete the connection wizard."))
        open_off_printer_profile_dialog()

    if off_printer_mode_enabled:
        refresh_off_printer_profile_state()
    refresh_print_state()
    _refresh_save_config_button()
    if not state.printer_is_printing:
        if off_printer_mode_enabled and not state.off_printer_profile_ready:
            state.deferred_startup_scan = True
            refresh_data()
        else:
            perform_index("startup")
    else:
        if off_printer_mode_enabled and not state.off_printer_profile_ready:
            state.deferred_startup_scan = True
        refresh_data()
    ui.timer(0.5, lambda: asyncio.create_task(_check_online_updates_on_startup()), once=True)
    ui.timer(0.5, _refresh_create_pr_progress_ui)
    ui.timer(0.5, _refresh_online_update_progress_ui)
    ui.timer(2.0, check_config_changes)
    ui.timer(5.0, refresh_off_printer_profile_state)

    def _flush_search() -> None:
        if state._search_dirty:
            state._search_dirty = False
            render_macro_list()

    ui.timer(0.25, _flush_search)

    with ui.footer().classes("items-center justify-end px-4 py-1 bg-grey-9 text-grey-3"):
        ui.label(f"KlipperVault v{app_version}").classes("text-xs")
