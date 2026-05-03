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
import shutil
import tempfile
import time
from urllib.parse import urlparse, urlunparse

from nicegui import ui

from klipper_macro_compare import MacroCompareView
from klipper_macro_gui_create_pr_flow import (
    begin_create_pr_request as _begin_create_pr_request,
    collect_create_pr_inputs as _collect_create_pr_inputs,
    finish_create_pr_request as _finish_create_pr_request,
    set_create_pr_request_failure as _set_create_pr_request_failure,
    validate_create_pr_inputs as _validate_create_pr_inputs,
)
from klipper_macro_gui_helpers import (
    _STATUS_BADGE_CLASSES,
    apply_theme_mode as _apply_theme_mode,
    default_keep_file as _default_keep_file,
    default_pr_head_branch as _default_pr_head_branch,
    dynamic_macro_file_paths as _dynamic_macro_file_paths,
    file_operation_phase_text as _file_operation_phase_text,
    format_moonraker_url_host as _format_moonraker_url_host,
    is_dynamic_version_row as _is_dynamic_version_row,
    is_remote_conflict_error as _is_remote_conflict_error,
    normalize_touched_cfg_paths as _normalize_touched_cfg_paths,
    normalized_theme_mode as _normalized_theme_mode,
    paths_include_dynamic_macros as _paths_include_dynamic_macros,
    printer_offline_status_text as _printer_offline_status_text,
    progress_value_and_percent as _progress_value_and_percent,
    reload_button_state as _reload_button_state,
    safe_notify as _safe_notify,
    save_config_button_enabled as _save_config_button_enabled,
    standard_profile_status as _standard_profile_status,
    status_badge_key as _status_badge_key,
    to_optional_int as _to_optional_int,
    translated_active_filter_state as _translated_active_filter_state,
)
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
from klipper_macro_gui_timers import register_periodic_updates
from klipper_macro_viewer import MacroViewer, format_ts as _format_ts
from klipper_type_utils import to_dict_list as _as_dict_list
from klipper_type_utils import to_int as _to_int
from klipper_vault_config import VaultConfig, load_or_create as _load_vault_config, save as _save_vault_config
from klipper_vault_paths import DEFAULT_CONFIG_DIR, DEFAULT_DB_PATH
from klipper_vault_i18n import set_language, t


def build_ui(app_version: str = "unknown", use_save_dialog: bool = False) -> None:
    """Build the full NiceGUI interface and wire all callbacks."""
    config_dir = Path(DEFAULT_CONFIG_DIR).expanduser().resolve()
    db_path = Path(DEFAULT_DB_PATH).expanduser().resolve()
    # Load (or create) app settings from SQLite once at startup.
    # All subsequent indexing runs read settings from this in-memory object.
    vault_cfg = _load_vault_config(config_dir, db_path)
    set_language(os.environ.get("KLIPPERVAULT_LANG", vault_cfg.ui_language))
    dark_mode = ui.dark_mode()
    _apply_theme_mode(dark_mode, str(vault_cfg.theme_mode or "auto"))
    ui.page_title(t("Klipper Vault"))
    runtime_mode = "standard"
    standard_mode_enabled = True
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
        standard_profile_ready=not standard_mode_enabled,
        list_page_size=max(50, _to_int(os.environ.get("KLIPPERVAULT_LIST_PAGE_SIZE", "200"), default=200)),
        last_activity_monotonic=time.monotonic(),
        duplicate_compare_view=MacroCompareView(),
    )
    _print_state_refresh_inflight = False
    _printer_card_status_refresh_inflight = False
    _macro_migration_prompt_shown = False
    _klipper_restart_grace_until: float = 0.0

    # ── Pre-declared local UI references (set by section builders below) ──────
    # These allow callbacks defined later to close over the same variables
    # that the builder functions assign. Python closures capture variables by
    # reference, so callbacks always see the post-builder values at call time.
    state.start_page_container: ui.column | None = None
    state.printer_editor_card: ui.card | None = None
    state.printer_editor_title: ui.label | None = None
    back_to_printers_button: ui.button | None = None
    macro_actions_button: ui.button | None = None
    macro_actions_menu: ui.menu | None = None
    macro_migration_menu_item: ui.menu_item | None = None
    macro_migration_menu_item_wrapper: ui.element | None = None
    reload_dynamic_macros_button: ui.button | None = None
    restart_klipper_button: ui.button | None = None
    duplicate_warning_button: ui.button | None = None
    save_config_button: ui.button | None = None
    index_button: ui.button | None = None
    settings_toolbar_button: ui.button | None = None
    state.start_page_status_label: ui.label | None = None
    state.refresh_printers_button: ui.button | None = None
    state.add_printer_button: ui.button | None = None
    state.test_active_printer_button: ui.button | None = None
    backup_name_input: ui.input | None = None
    backup_error_label: ui.label | None = None
    create_backup_button: ui.button | None = None
    backup_view_title: ui.label | None = None
    backup_view_subtitle: ui.label | None = None
    backup_view_table: ui.table | None = None
    # Early dialog variables
    printer_connecting_dialog: ui.dialog | None = None
    printer_connecting_label: ui.label | None = None
    file_operation_dialog: ui.dialog | None = None
    file_operation_title: ui.label | None = None
    file_operation_phase: ui.label | None = None
    file_operation_percent: ui.label | None = None
    file_operation_progress: ui.linear_progress | None = None
    printer_profile_dialog: ui.dialog | None = None
    printer_vendor_input: ui.input | None = None
    printer_model_input: ui.input | None = None
    printer_profile_error: ui.label | None = None
    save_printer_profile_button: ui.button | None = None
    app_settings_dialog: ui.dialog | None = None
    settings_version_history_input: ui.number | None = None
    settings_language_select: ui.select | None = None
    settings_theme_mode_select: ui.select | None = None
    settings_repo_url_input: ui.input | None = None
    settings_ref_input: ui.input | None = None
    settings_developer_toggle: ui.switch | None = None
    settings_error_label: ui.label | None = None
    settings_info_label: ui.label | None = None
    save_settings_button: ui.button | None = None
    macro_migration_prompt_dialog: ui.dialog | None = None
    macro_migration_prompt_message: ui.label | None = None
    macro_migration_prompt_error: ui.label | None = None
    confirm_macro_migration_button: ui.button | None = None
    decline_macro_migration_button: ui.button | None = None
    # Phase 4: Macro operation dialogs variables
    load_order_dialog: ui.dialog | None = None
    restore_dialog: ui.dialog | None = None
    restore_error_label: ui.label | None = None
    delete_dialog: ui.dialog | None = None
    delete_error_label: ui.label | None = None
    printer_delete_dialog: ui.dialog | None = None
    printer_delete_dialog_title: ui.label | None = None
    printer_delete_error_label: ui.label | None = None
    confirm_printer_delete_button: ui.button | None = None
    export_dialog: ui.dialog | None = None
    export_macro_list: ui.list | None = None
    export_error_label: ui.label | None = None
    import_dialog: ui.dialog | None = None
    import_error_label: ui.label | None = None
    import_cfg_dialog: ui.dialog | None = None
    import_cfg_error_label: ui.label | None = None
    create_pr_dialog: ui.dialog | None = None
    create_pr_error_label: ui.label | None = None
    create_virtual_printer_dialog: ui.dialog | None = None
    virtual_printer_name_input: ui.input | None = None
    virtual_printer_vendor_input: ui.input | None = None
    virtual_printer_model_input: ui.input | None = None
    create_virtual_printer_error_label: ui.label | None = None
    load_order_summary_label: ui.label | None = None
    load_order_text: ui.label | None = None
    confirm_restore_button: ui.button | None = None
    confirm_delete_button: ui.button | None = None
    confirm_export_button: ui.button | None = None
    confirm_import_button: ui.button | None = None
    confirm_import_cfg_button: ui.button | None = None
    confirm_create_pr_button: ui.button | None = None
    confirm_create_virtual_printer_button: ui.button | None = None
    create_pr_progress_label: ui.label | None = None
    create_pr_progress_bar: ui.linear_progress | None = None
    export_macro_checkboxes: dict[str, object] = {}
    save_path_dialog: ui.dialog | None = None
    save_path_input: ui.input | None = None
    save_path_error_label: ui.label | None = None
    _save_path_pending_src: Path | None = None
    # Phase 5: Remote dialogs variables
    online_update_dialog: ui.dialog | None = None
    online_update_progress_label: ui.label | None = None
    online_update_progress_bar: ui.linear_progress | None = None
    online_update_summary_label: ui.label | None = None
    online_update_list: ui.column | None = None
    online_update_error_label: ui.label | None = None
    confirm_online_update_button: ui.button | None = None
    duplicate_wizard_dialog: ui.dialog | None = None
    duplicate_wizard_title: ui.label | None = None
    duplicate_wizard_subtitle: ui.label | None = None
    duplicate_entry_list: ui.list | None = None
    duplicate_keep_select: ui.select | None = None
    duplicate_compare_with_select: ui.select | None = None
    duplicate_compare_button: ui.button | None = None
    duplicate_wizard_error: ui.label | None = None
    duplicate_prev_button: ui.button | None = None
    duplicate_next_button: ui.button | None = None
    duplicate_apply_button: ui.button | None = None
    remote_cfg_list_dialog: ui.dialog | None = None
    remote_cfg_list_title: ui.label | None = None
    remote_cfg_list_subtitle: ui.label | None = None
    remote_cfg_list_text: ui.textarea | None = None
    remote_cfg_list_error: ui.label | None = None
    remote_conflict_dialog: ui.dialog | None = None
    remote_conflict_dialog_guidance: ui.label | None = None
    remote_conflict_dialog_detail: ui.label | None = None
    sync_after_conflict_button: ui.button | None = None
    developer_menu_import_cfg_item: ui.menu_item | None = None
    developer_menu_export_update_item: ui.menu_item | None = None
    developer_menu_create_pr_item: ui.menu_item | None = None

    # ── Section builder: toolbar ──────────────────────────────────────────────
    def _build_toolbar() -> None:
        """Build the top header toolbar and store element references on state."""
        nonlocal back_to_printers_button, settings_toolbar_button
        nonlocal macro_actions_button, macro_actions_menu
        nonlocal reload_dynamic_macros_button, restart_klipper_button, duplicate_warning_button
        nonlocal save_config_button, index_button
        with ui.header().classes("items-center gap-2 px-4 py-2 bg-grey-9 flex-wrap") as toolbar_header:
            state.toolbar_header = toolbar_header
            ui.label(t("Klipper Vault")).classes("text-xl font-bold text-white")
            back_to_printers_button = ui.button(t("Back to printers"), icon="arrow_back").props("flat color=white")
            back_to_printers_button.set_visibility(False)
            ui.space()
            with ui.button(t("Macro actions"), icon="menu").props("flat color=white") as macro_actions_button_ref:
                macro_actions_button = macro_actions_button_ref
                state.macro_actions_button = macro_actions_button
                with ui.menu() as macro_actions_menu_ref:
                    macro_actions_menu = macro_actions_menu_ref
                    state.macro_actions_menu = macro_actions_menu
                    pass
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

    def _active_printer_is_virtual() -> bool:
        """Return True when currently active printer profile is virtual/local-only."""
        profile = service.get_active_printer_profile()
        return bool(profile.get("is_virtual", False)) if isinstance(profile, dict) else False

    # ── Section builder: macro page layout ───────────────────────────────────
    def _build_macro_page() -> None:
        """Build the macro workspace page (macro list, viewer, statistics cards)."""
        with ui.column().classes("w-full") as macro_page_container_ref:
            state.macro_page_container = macro_page_container_ref
            with ui.grid().classes("w-full grid-cols-1 md:grid-cols-3 xl:grid-cols-4 gap-4 p-4 xl:h-[calc(100vh-110px)]"):
                with ui.card().classes("col-span-1 xl:h-full flex flex-col overflow-hidden min-h-[55vh] xl:min-h-0"):
                    ui.label(t("Indexed macros")).classes("text-lg font-semibold mb-2 shrink-0")
                    search_input = ui.input(placeholder=t("Search macros…")).props("clearable dense outlined").classes("w-full mb-1 shrink-0")
                    with ui.row().classes("items-center gap-2 mb-1 shrink-0"):
                        state.duplicates_button = ui.button(t("Show duplicates")).props("flat dense no-caps")
                        state.new_button = ui.button(t("Show new")).props("flat dense no-caps")
                        state.active_filter_button = ui.button(t("Filter: {state}", state=t("all"))).props("flat dense no-caps")
                    with ui.row().classes("items-center gap-1 mb-1 shrink-0"):
                        ui.label(t("Sort:")).classes("text-xs text-grey-4")
                        state.sort_radio = (
                            ui.radio(
                                options={"load_order": t("Load order"), "alpha_asc": "A → Z", "alpha_desc": "Z → A"},
                                value="load_order",
                            )
                            .props("inline dense")
                            .classes("text-xs")
                        )
                    state.macro_count_label = ui.label(t("Items: {visible}", visible=0)).classes("text-sm text-grey-4 shrink-0")
                    with ui.row().classes("items-center gap-2 mb-1 shrink-0"):
                        state.prev_page_button = ui.button(t("Prev"))
                        state.prev_page_button.props("flat dense no-caps")
                        state.next_page_button = ui.button(t("Next"))
                        state.next_page_button.props("flat dense no-caps")
                    state.macro_list = ui.list().props("separator").classes("w-full overflow-y-auto flex-1 min-h-0")
                    state.macro_search = search_input

                state.viewer = MacroViewer()

                with ui.card().classes("col-span-1 md:col-span-3 xl:col-span-1 xl:h-full overflow-auto"):
                    ui.label(t("Stored macro statistics")).classes("text-lg font-semibold")
                    with ui.column().classes("gap-2 mt-2"):
                        state.total_macros_label = ui.label(t("Total macros: {count}", count=0))
                        state.duplicate_macros_label = ui.label(t("Duplicate macros: {count}", count=0))
                        state.deleted_macros_label = ui.label(t("Deleted macros: {count}", count=0))
                        state.distinct_files_label = ui.label(t("Config files: {count}", count=0))
                        state.last_update_label = ui.label(t("Last update: never"))
                        state.purge_deleted_button = ui.button(t("Remove all deleted macros")).props(
                            "flat color=negative no-caps"
                        )
                        state.purge_deleted_button.set_visibility(False)

                    ui.separator().classes("my-2")
                    ui.label(t("Status")).classes("text-md font-semibold mb-1")
                    state.status_label = ui.label(t("Ready")).classes("text-sm text-grey-4")
                    state.standard_profile_label = ui.label("").classes("text-xs text-grey-5")
                    state.standard_profile_label.set_visibility(standard_mode_enabled)
                    with ui.row().classes("items-center gap-2"):
                        state.standard_cfg_list_button = ui.button(t("Show remote cfg files")).props("flat dense no-caps")
                        state.standard_cfg_list_button.set_visibility(standard_mode_enabled and (not _active_printer_is_virtual()))
                    ui.separator().classes("my-2")
                    ui.label(t("Backups")).classes("text-md font-semibold mb-1")
                    state.backup_list = ui.list().props("separator").classes("w-full max-h-[46vh] overflow-y-auto")

    # ── Section builder: start page and printer editor ────────────────────────
    def _build_start_page() -> None:
        """Build the start page (printer selector and editor)."""
        with ui.column().classes("w-full p-4 gap-4") as start_page_container_ref:
            state.start_page_container = start_page_container_ref
            ui.label(t("Select printer")).classes("text-2xl font-semibold")
            ui.label(t("Choose a configured printer to open the macro workspace.")).classes("text-sm text-grey-5")
            state.start_page_status_label = ui.label("").classes("text-sm text-grey-4")
            with ui.row().classes("w-full gap-3 items-center"):
                state.refresh_printers_button = ui.button(t("Refresh printers"), icon="refresh").props("flat no-caps")
                state.add_printer_button = ui.button(t("Add printer"), icon="add").props("flat no-caps")
                state.test_active_printer_button = ui.button(t("Test active connection"), icon="network_check").props("flat no-caps")
            state.printer_cards_container = ui.row().classes("w-full gap-4 flex-wrap")

        with state.start_page_container:
            with ui.card().classes("w-full") as printer_editor_card_ref:
                state.printer_editor_card = printer_editor_card_ref
                state.printer_editor_title = ui.label(t("Printer connection management")).classes("text-lg font-semibold")
                ui.label(t("Configure SSH settings as part of each printer profile for standard mode.")).classes("text-sm text-grey-5")
                state.ssh_profile_select = ui.select(options=[], label=t("Saved profiles")).props("outlined dense").classes("w-full mt-2")
                with ui.row().classes("w-full gap-2"):
                    state.ssh_profile_name_input = ui.input(label=t("Profile name")).props("outlined dense").classes("flex-1")
                    state.ssh_profile_host_input = ui.input(label=t("Host"), on_change=lambda e: _sync_moonraker_url_host(str(e.value or ""))).props("outlined dense").classes("flex-1")
                with ui.row().classes("w-full gap-2"):
                    state.ssh_profile_port_input = ui.number(label=t("Port"), value=22).props("outlined dense").classes("w-32")
                    state.ssh_profile_username_input = ui.input(label=t("Username")).props("outlined dense").classes("flex-1")
                state.ssh_profile_remote_dir_input = ui.input(label=t("Remote config directory"), value="~/printer_data/config").props(
                    "outlined dense"
                ).classes("w-full")
                state.ssh_profile_moonraker_url_input = ui.input(
                    label=t("Moonraker URL"), value="http://127.0.0.1:7125"
                ).props("outlined dense").classes("w-full")
                state.ssh_profile_auth_mode_select = ui.select(
                    options={"key": t("SSH key"), "password": t("Password")},
                    value="password",
                    label=t("Authentication mode"),
                ).props("outlined dense").classes("w-full")
                state.ssh_profile_secret_input = ui.input(label=t("SSH password/secret")).props("outlined dense type=text").classes(
                    "w-full"
                )
                state.ssh_profile_secret_mode_label = ui.label("").classes("text-xs text-grey-5")
                state.ssh_profile_secret_state_label = ui.label("").classes("text-xs text-grey-5")
                state.ssh_profile_active_toggle = ui.switch(t("Set as active profile"), value=True)
                state.ssh_profile_error_label = ui.label("").classes("text-sm text-negative mt-1")
                state.ssh_profile_status_label = ui.label("").classes("text-sm text-positive mt-1")
                with ui.row().classes("w-full justify-between mt-3"):
                    with ui.row().classes("gap-2"):
                        state.hide_printer_editor_button = ui.button(t("Close editor")).props("flat no-caps")
                        state.refresh_ssh_profiles_button = ui.button(t("Refresh")).props("flat no-caps")
                        state.new_ssh_profile_button = ui.button(t("Add printer")).props("flat no-caps")
                    with ui.row().classes("gap-2"):
                        state.delete_ssh_profile_button = ui.button(t("Delete selected")).props("flat color=negative no-caps")
                        state.activate_ssh_profile_button = ui.button(t("Activate selected")).props("flat no-caps")
                        state.save_ssh_profile_button = ui.button(t("Save profile")).props("color=primary no-caps")
            printer_editor_card_ref.set_visibility(False)

    # ── Section builder: early dialogs ────────────────────────────────────────
    def _build_early_dialogs() -> None:
        """Build early dialogs: printer connecting, file operations, printer profile, app settings."""
        nonlocal printer_connecting_dialog, printer_connecting_label
        nonlocal file_operation_dialog, file_operation_title, file_operation_phase, file_operation_percent, file_operation_progress
        nonlocal printer_profile_dialog, printer_vendor_input, printer_model_input, printer_profile_error, save_printer_profile_button
        nonlocal app_settings_dialog, settings_version_history_input, settings_language_select, settings_theme_mode_select
        nonlocal settings_repo_url_input, settings_ref_input, settings_developer_toggle
        nonlocal settings_error_label, settings_info_label, save_settings_button
        nonlocal macro_migration_prompt_dialog, macro_migration_prompt_message, macro_migration_prompt_error
        nonlocal confirm_macro_migration_button, decline_macro_migration_button

        with ui.dialog().props("persistent") as printer_connecting_dialog_ref, ui.card().classes("w-[30rem] max-w-[94vw]"):
            printer_connecting_dialog = printer_connecting_dialog_ref
            ui.label(t("Connecting to printer...")).classes("text-lg font-semibold")
            printer_connecting_label = ui.label(
                t("KlipperVault is reconnecting. The dialog closes automatically when the UI is responsive again.")
            ).classes("text-sm text-grey-5")
            state.printer_connecting_dialog = printer_connecting_dialog_ref
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

        state._set_printer_connecting_modal = _set_printer_connecting_modal

        with ui.dialog().props("persistent") as file_operation_dialog_ref, ui.card().classes("w-[34rem] max-w-[96vw]"):
            file_operation_dialog = file_operation_dialog_ref
            file_operation_title = ui.label(t("Working on files")).classes("text-lg font-semibold")
            file_operation_phase = ui.label("").classes("text-sm text-grey-5")
            file_operation_percent = ui.label("0%").classes("text-sm text-grey-5 mt-1")
            file_operation_progress = ui.linear_progress(value=0.0, show_value=False).classes("w-full mt-1")

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

        async def _run_with_file_operation_modal(title_text: str, action):
            """Run a blocking file operation in a thread while showing a progress modal."""
            file_operation_title.set_text(str(title_text))
            _set_file_operation_progress("", 0, 1)
            file_operation_dialog.open()
            await asyncio.sleep(0)
            try:
                return await asyncio.to_thread(action)
            finally:
                file_operation_dialog.close()

        state._set_file_operation_progress = _set_file_operation_progress
        state._run_with_file_operation_modal = _run_with_file_operation_modal

        with ui.dialog().props("persistent") as printer_profile_dialog_ref, ui.card().classes("w-[34rem] max-w-[96vw]"):
            printer_profile_dialog = printer_profile_dialog_ref
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

        with ui.dialog() as app_settings_dialog_ref, ui.card().classes("w-[42rem] max-w-[96vw]"):
            app_settings_dialog = app_settings_dialog_ref
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
            settings_theme_mode_select = (
                ui.select(
                    options={
                        "auto": t("Auto (follow system/browser)"),
                        "light": t("Light"),
                        "dark": t("Dark"),
                    },
                    value=_normalized_theme_mode(vault_cfg.theme_mode),
                    label=t("Theme mode"),
                )
                .props("outlined dense")
                .classes("w-full")
            )
            settings_repo_url_input = ui.input(label=t("Online update repository URL")).props("outlined dense").classes("w-full")
            settings_ref_input = ui.input(label=t("Online update reference")).props("outlined dense").classes("w-full")
            settings_developer_toggle = ui.switch(t("Developer mode"), value=bool(vault_cfg.developer))
            settings_error_label = ui.label("").classes("text-sm text-negative mt-1")
            settings_info_label = ui.label("").classes("text-sm text-grey-5 mt-1")
            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                flat_dialog_button("Cancel", app_settings_dialog_ref.close)
                save_settings_button = ui.button(t("Save")).props("color=primary no-caps")

        with ui.dialog().props("persistent") as macro_migration_prompt_dialog_ref, ui.card().classes("w-[36rem] max-w-[96vw]"):
            macro_migration_prompt_dialog = macro_migration_prompt_dialog_ref
            ui.label(t("Macro migration available")).classes("text-lg font-semibold")
            macro_migration_prompt_message = ui.label("").classes("text-sm text-grey-5")
            macro_migration_prompt_error = ui.label("").classes("text-sm text-negative mt-1")
            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                decline_macro_migration_button = ui.button(t("Don't ask again")).props("flat no-caps")
                confirm_macro_migration_button = ui.button(t("Migrate now")).props("color=primary no-caps")

    # ── Section builder: macro operation dialogs ─────────────────────────────
    def _build_macro_operation_dialogs() -> None:
        """Build macro operation dialogs: load order, restore, delete, export, import, create PR."""
        nonlocal load_order_dialog, restore_dialog, delete_dialog, export_dialog, import_dialog, import_cfg_dialog, create_pr_dialog
        nonlocal create_virtual_printer_dialog, virtual_printer_name_input, virtual_printer_vendor_input
        nonlocal virtual_printer_model_input, create_virtual_printer_error_label
        nonlocal load_order_summary_label, load_order_text
        nonlocal restore_error_label, delete_error_label, export_macro_list, export_error_label, import_error_label, import_cfg_error_label
        nonlocal confirm_restore_button, confirm_delete_button, confirm_export_button, confirm_import_button, confirm_import_cfg_button
        nonlocal confirm_create_pr_button, confirm_create_virtual_printer_button
        nonlocal create_pr_progress_label, create_pr_progress_bar, create_pr_error_label
        nonlocal export_macro_checkboxes
        nonlocal save_path_dialog, save_path_input, save_path_error_label
        nonlocal printer_delete_dialog, printer_delete_dialog_title, printer_delete_error_label, confirm_printer_delete_button

        with ui.dialog() as load_order_dialog_ref, ui.card().classes(
            "w-[56rem] max-w-[98vw] h-[86vh] max-h-[94vh] flex flex-col overflow-hidden"
        ):
            load_order_dialog = load_order_dialog_ref
            with ui.row().classes("w-full items-center justify-between"):
                ui.button(icon="close", on_click=load_order_dialog_ref.close).props("flat dense round")
                ui.label(t("Klipper loading order overview")).classes("text-lg font-semibold")
                ui.space()
            load_order_summary_label = ui.label("").classes("text-sm text-grey-5")
            ui.label(t("Klipper parse order")).classes("text-sm font-semibold mt-2")
            load_order_text = ui.label("").classes(
                "w-full flex-1 overflow-y-auto whitespace-pre-wrap break-words border border-grey-8 rounded p-3 font-mono text-sm mt-2"
            )

            with ui.row().classes("w-full items-center mt-3"):
                ui.space()
                flat_dialog_button("Close", load_order_dialog_ref.close)
        state.load_order_dialog = load_order_dialog_ref

        with ui.dialog() as restore_dialog_ref, ui.card().classes("w-[30rem] max-w-[96vw]"):
            restore_dialog = restore_dialog_ref
            ui.label(t("Restore backup")).classes("text-lg font-semibold")
            restore_confirm_label = ui.label("").classes("text-sm text-grey-5")
            restore_error_label = ui.label("").classes("text-sm text-negative mt-1")
            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                flat_dialog_button("Cancel", restore_dialog_ref.close)
                confirm_restore_button = ui.button(t("Restore")).props("color=warning no-caps")
            state.restore_dialog = restore_dialog_ref
            state.restore_confirm_label = restore_confirm_label
            state.restore_error_label = restore_error_label

        with ui.dialog() as delete_dialog_ref, ui.card().classes("w-[30rem] max-w-[96vw]"):
            delete_dialog = delete_dialog_ref
            ui.label(t("Delete backup")).classes("text-lg font-semibold")
            delete_confirm_label = ui.label("").classes("text-sm text-grey-5")
            delete_error_label = ui.label("").classes("text-sm text-negative mt-1")
            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                flat_dialog_button("Cancel", delete_dialog_ref.close)
                confirm_delete_button = ui.button(t("Delete")).props("color=negative no-caps")
            state.delete_dialog = delete_dialog_ref
            state.delete_confirm_label = delete_confirm_label
            state.delete_error_label = delete_error_label

        with ui.dialog() as printer_delete_dialog_ref, ui.card().classes("w-[30rem] max-w-[96vw]"):
            printer_delete_dialog = printer_delete_dialog_ref
            printer_delete_dialog_title = ui.label("").classes("text-lg font-semibold")
            ui.label(t("This will delete all data associated with this printer, including all macro backups.")).classes("text-sm text-grey-5")
            printer_delete_error_label = ui.label("").classes("text-sm text-negative mt-1")
            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                flat_dialog_button("Cancel", printer_delete_dialog_ref.close)
                confirm_printer_delete_button = ui.button(t("Delete Printer")).props("color=negative no-caps")
            state.printer_delete_dialog = printer_delete_dialog_ref
            state.printer_delete_dialog_title = printer_delete_dialog_title
            state.printer_delete_error_label = printer_delete_error_label
            state.confirm_printer_delete_button = confirm_printer_delete_button
            state.printer_delete_profile_id = 0

        with ui.dialog() as export_dialog_ref, ui.card().classes("w-[42rem] max-w-[98vw]"):
            export_dialog = export_dialog_ref
            ui.label(t("Export macros")).classes("text-lg font-semibold")
            ui.label(t("Select one or more macros to export into a share file.")).classes("text-sm text-grey-5")
            ui.label(t("Macros")).classes("text-sm mt-2")
            export_macro_list = ui.column().classes("w-full max-h-[20rem] overflow-y-auto gap-1 border rounded p-2")
            export_error_label = ui.label("").classes("text-sm text-negative mt-1")
            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                flat_dialog_button("Cancel", export_dialog_ref.close)
                confirm_export_button = ui.button(t("Export")).props("color=primary no-caps")
        state.export_dialog = export_dialog_ref
        state.export_macro_list = export_macro_list

        export_macro_checkboxes = {}

        with ui.dialog().props("persistent") as save_path_dialog_ref, ui.card().classes("w-[42rem] max-w-[98vw]"):
            save_path_dialog = save_path_dialog_ref
            ui.label(t("Save exported file")).classes("text-lg font-semibold")
            ui.label(t("Choose where to save the exported file on this computer.")).classes("text-sm text-grey-5")
            save_path_input = ui.input(label=t("Save path")).props("outlined").classes("w-full mt-2")
            save_path_error_label = ui.label("").classes("text-sm text-negative mt-1")
            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                flat_dialog_button("Cancel", save_path_dialog_ref.close)
                ui.button(t("Save"), on_click=lambda: _confirm_save_path()).props("color=primary no-caps")
        state.save_path_dialog = save_path_dialog_ref
        state.save_path_input = save_path_input
        state.save_path_error_label = save_path_error_label

        with ui.dialog() as import_dialog_ref, ui.card().classes("w-[38rem] max-w-[98vw]"):
            import_dialog = import_dialog_ref
            ui.label(t("Import macros")).classes("text-lg font-semibold")
            ui.label(t("Import a shared macro file into inactive new versions.")).classes("text-sm text-grey-5")
            import_file_input = ui.upload(on_upload=_on_import_upload, auto_upload=True).props("accept=.json").classes("w-full mt-2")
            import_error_label = ui.label("").classes("text-sm text-negative mt-1")
            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                flat_dialog_button("Cancel", import_dialog_ref.close)
                confirm_import_button = ui.button(t("Import")).props("color=primary no-caps")
        state.import_dialog = import_dialog_ref
        state.import_uploader = import_file_input
        state.import_error_label = import_error_label

        with ui.dialog() as import_cfg_dialog_ref, ui.card().classes("w-[38rem] max-w-[98vw]"):
            import_cfg_dialog = import_cfg_dialog_ref
            ui.label(t("Import macro.cfg")).classes("text-lg font-semibold")
            ui.label(t("Upload one local .cfg file into the virtual printer workspace.")).classes("text-sm text-grey-5")
            import_cfg_file_input = ui.upload(on_upload=_on_import_cfg_upload, auto_upload=True).props("accept=.cfg").classes("w-full mt-2")
            import_cfg_error_label = ui.label("").classes("text-sm text-negative mt-1")
            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                flat_dialog_button("Cancel", import_cfg_dialog_ref.close)
                confirm_import_cfg_button = ui.button(t("Import cfg")).props("color=primary no-caps")
        state.import_cfg_dialog = import_cfg_dialog_ref
        state.import_cfg_uploader = import_cfg_file_input
        state.import_cfg_error_label = import_cfg_error_label

        with ui.dialog() as create_pr_dialog_ref, ui.card().classes("w-[46rem] max-w-[98vw]"):
            create_pr_dialog = create_pr_dialog_ref
            ui.label(t("Create Pull Request")).classes("text-lg font-semibold")
            ui.label(t("Publish active macros directly to GitHub and open a pull request.")).classes("text-sm text-grey-5")
            ui.label(
                t("Pull requests always write a printer-local manifest at [vendor]/[model]/manifest.json.")
            ).classes("text-xs text-grey-5")
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
                flat_dialog_button("Cancel", create_pr_dialog_ref.close)
                confirm_create_pr_button = ui.button(t("Create PR")).props("color=primary no-caps")
            state.create_pr_dialog = create_pr_dialog_ref
            state.pr_repo_url_input = pr_repo_url_input
            state.pr_base_branch_input = pr_base_branch_input
            state.pr_head_branch_input = pr_head_branch_input
            state.pr_title_input = pr_title_input
            state.pr_body_input = pr_body_input
            state.pr_token_input = pr_token_input
            state.create_pr_error_label = create_pr_error_label
            state.confirm_create_pr_button = confirm_create_pr_button

        with ui.dialog() as create_virtual_printer_dialog_ref, ui.card().classes("w-[32rem] max-w-[96vw]"):
            create_virtual_printer_dialog = create_virtual_printer_dialog_ref
            ui.label(t("Create Virtual Printer")).classes("text-lg font-semibold")
            ui.label(
                t("Create a local-only developer profile for a printer you do not physically own.")
            ).classes("text-sm text-grey-5")
            virtual_printer_name_input = ui.input(label=t("Profile name")).props("outlined").classes("w-full mt-2")
            with ui.row().classes("w-full gap-2"):
                virtual_printer_vendor_input = ui.input(label=t("Vendor")).props("outlined").classes("w-full")
                virtual_printer_model_input = ui.input(label=t("Model")).props("outlined").classes("w-full")
            create_virtual_printer_error_label = ui.label("").classes("text-sm text-negative mt-1")
            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                flat_dialog_button("Cancel", create_virtual_printer_dialog_ref.close)
                confirm_create_virtual_printer_button = ui.button(t("Create Virtual Printer")).props("color=primary no-caps")

    # ── Section builder: remote dialogs ──────────────────────────────────────
    def _build_remote_dialogs() -> None:
        """Build remote dialogs: online update, duplicate wizard, remote cfg, remote conflict."""
        nonlocal online_update_dialog, online_update_progress_label, online_update_progress_bar
        nonlocal online_update_summary_label, online_update_list, online_update_error_label, confirm_online_update_button
        nonlocal duplicate_wizard_dialog, duplicate_wizard_title, duplicate_wizard_subtitle
        nonlocal duplicate_entry_list, duplicate_keep_select, duplicate_compare_with_select, duplicate_compare_button
        nonlocal duplicate_wizard_error, duplicate_prev_button, duplicate_next_button, duplicate_apply_button
        nonlocal remote_cfg_list_dialog, remote_cfg_list_title, remote_cfg_list_subtitle, remote_cfg_list_text, remote_cfg_list_error
        nonlocal remote_conflict_dialog, remote_conflict_dialog_guidance, remote_conflict_dialog_detail, sync_after_conflict_button

        with ui.dialog() as online_update_dialog_ref, ui.card().classes("w-[46rem] max-w-[98vw]"):
            online_update_dialog = online_update_dialog_ref
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
                flat_dialog_button("Cancel", online_update_dialog_ref.close)
                confirm_online_update_button = ui.button(t("Import updates")).props("color=primary no-caps")
                confirm_online_update_button.set_visibility(False)
            state.online_update_dialog = online_update_dialog_ref
            state.online_update_list = online_update_list
            state.online_update_summary_label = online_update_summary_label
            state.online_update_error_label = online_update_error_label
            state.confirm_online_update_button = confirm_online_update_button

        with ui.dialog() as duplicate_wizard_dialog_ref, ui.card().classes("w-[48rem] max-w-[98vw]"):
            duplicate_wizard_dialog = duplicate_wizard_dialog_ref
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
                    flat_dialog_button("Cancel", duplicate_wizard_dialog_ref.close)
                    duplicate_next_button = ui.button(t("Next")).props("flat no-caps")
                    duplicate_apply_button = ui.button(t("Apply")).props("color=warning no-caps")
            state.duplicate_wizard_dialog = duplicate_wizard_dialog_ref
            state.duplicate_wizard_title = duplicate_wizard_title
            state.duplicate_wizard_subtitle = duplicate_wizard_subtitle
            state.duplicate_wizard_error = duplicate_wizard_error

        with ui.dialog() as remote_cfg_list_dialog_ref, ui.card().classes("w-[52rem] max-w-[98vw] h-[82vh] max-h-[92vh] flex flex-col"):
            remote_cfg_list_dialog = remote_cfg_list_dialog_ref
            remote_cfg_list_title = ui.label(t("Remote cfg files")).classes("text-lg font-semibold")
            remote_cfg_list_subtitle = ui.label("").classes("text-sm text-grey-5")
            remote_cfg_list_text = ui.textarea(label=t("Files"), value="").props("readonly autogrow").classes(
                "w-full flex-1 mt-2"
            )
            remote_cfg_list_error = ui.label("").classes("text-sm text-negative mt-1")
            with ui.row().classes("w-full justify-end mt-3"):
                flat_dialog_button("Close", remote_cfg_list_dialog_ref.close)

        with ui.dialog() as remote_conflict_dialog_ref, ui.card().classes("w-[40rem] max-w-[96vw]"):
            remote_conflict_dialog = remote_conflict_dialog_ref
            ui.label(t("Remote changes detected")).classes("text-lg font-semibold text-warning")
            remote_conflict_dialog_guidance = ui.label("").classes("text-sm text-grey-4 mt-1")
            remote_conflict_dialog_detail = ui.label("").classes("text-sm text-negative mt-1")
            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                flat_dialog_button("Close", remote_conflict_dialog_ref.close)
                sync_after_conflict_button = ui.button(t("Sync and reload")).props("color=primary no-caps")

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

    async def _on_import_cfg_upload(e) -> None:
        """Capture uploaded cfg file contents for virtual-printer runtime import."""
        uploaded_file = getattr(e, "file", None)
        if uploaded_file is None:
            state.uploaded_cfg_import_bytes = None
            state.uploaded_cfg_import_name = ""
            state.import_cfg_error_label.set_text(t("Please upload a .cfg file."))
            return

        state.uploaded_cfg_import_bytes = await uploaded_file.read()
        state.uploaded_cfg_import_name = str(getattr(uploaded_file, "name", "") or "")
        state.import_cfg_error_label.set_text("")

    # ── Build top toolbar ─────────────────────────────────────────────────────
    _build_toolbar()

    # ── Build macro page layout ───────────────────────────────────────────────
    _build_macro_page()

    # ── Build start page and printer editor ────────────────────────────────────
    _build_start_page()

    # ── Build early dialogs ───────────────────────────────────────────────────
    _build_early_dialogs()

    # ── Exported-file delivery helpers ────────────────────────────────────────
    def _deliver_exported_file(src: Path) -> None:
        """Deliver an exported file: browser download on Linux/Docker, save dialog on Windows/macOS binary."""
        if use_save_dialog:
            state._save_path_pending_src = src
            default_dest = Path.home() / "Downloads" / src.name
            state.save_path_input.set_value(str(default_dest))
            state.save_path_error_label.set_text("")
            state.save_path_dialog.open()
        else:
            ui.download(src, filename=src.name)

    def _confirm_save_path() -> None:
        """Copy the pending export file to the user-chosen path and close the dialog."""
        raw = str(state.save_path_input.value or "").strip()
        if not raw:
            state.save_path_error_label.set_text(t("Please enter a save path."))
            return
        dest = Path(raw)
        src = state._save_path_pending_src
        if src is None or not src.exists():
            state.save_path_error_label.set_text(t("Source file is no longer available."))
            return
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        except Exception as exc:
            state.save_path_error_label.set_text(t("Failed to save file: {error}", error=exc))
            return
        state.save_path_dialog.close()
        state._save_path_pending_src = None
        state.status_label.set_text(t("File saved to {path}.", path=str(dest)))

    # ── Build macro operation dialogs ─────────────────────────────────────────
    _build_macro_operation_dialogs()

    # ── Build remote dialogs ─────────────────────────────────────────────────
    _build_remote_dialogs()

    # All state is now managed through the UIState container.
    # Callbacks access state directly via closure capture of the state object.

    def _note_activity() -> None:
        """Record runtime activity for UI/background flow control."""
        state.last_activity_monotonic = time.monotonic()

    def _ui_still_available() -> bool:
        """Return True while this page/client still has live target elements."""
        if state.status_label is None or state.status_label.is_deleted:
            return False
        if state.macro_list is None or state.macro_list.is_deleted:
            return False
        if state.backup_list is None or state.backup_list.is_deleted:
            return False
        return True

    def _set_view(view: str) -> None:
        """Switch between printer start page and macro workspace."""
        normalized = "macro" if str(view).strip().lower() == "macro" else "start"
        state.current_view = normalized
        is_macro = normalized == "macro"
        if state.start_page_container is not None:
            state.start_page_container.set_visibility(not is_macro)
        if state.macro_page_container is not None:
            state.macro_page_container.set_visibility(is_macro)
        back_to_printers_button.set_visibility(is_macro)
        if state.macro_actions_button is not None:
            state.macro_actions_button.set_visibility(is_macro)
        if state.reload_dynamic_macros_button is not None:
            if not is_macro:
                state.reload_dynamic_macros_button.set_visibility(False)
        # Hide macro-only developer menu items on start page
        if developer_menu_import_cfg_item is not None:
            developer_menu_import_cfg_item.set_visibility(is_macro)
        if developer_menu_export_update_item is not None:
            developer_menu_export_update_item.set_visibility(is_macro)
        if developer_menu_create_pr_item is not None:
            developer_menu_create_pr_item.set_visibility(is_macro)
        if state.restart_klipper_button is not None:
            if not is_macro:
                state.restart_klipper_button.set_visibility(False)
        if state.duplicate_warning_button is not None:
            if not is_macro:
                state.duplicate_warning_button.set_visibility(False)
        if state.save_config_button is not None:
            state.save_config_button.set_visibility(is_macro)
        if state.index_button is not None:
            state.index_button.set_visibility(is_macro)
        _refresh_macro_migration_action_visibility()

    def _persist_macro_migration_prompt_enabled(enabled: bool) -> bool:
        """Persist one-time migration prompt preference to SQLite settings."""
        next_cfg = VaultConfig(
            version_history_size=int(vault_cfg.version_history_size),
            port=int(vault_cfg.port),
            runtime_mode=str(vault_cfg.runtime_mode or "standard"),
            ui_language=str(vault_cfg.ui_language or "en"),
            printer_vendor=str(vault_cfg.printer_vendor or ""),
            printer_model=str(vault_cfg.printer_model or ""),
            online_update_repo_url=str(vault_cfg.online_update_repo_url or "").strip(),
            online_update_ref=str(vault_cfg.online_update_ref or "").strip(),
            theme_mode=str(vault_cfg.theme_mode or "auto"),
            developer=bool(vault_cfg.developer),
            printer_profile_prompt_required=bool(vault_cfg.printer_profile_prompt_required),
            macro_migration_prompt_enabled=bool(enabled),
        )
        try:
            _save_vault_config(config_dir, next_cfg, db_path)
        except Exception as exc:
            state.status_label.set_text(t("Failed to save migration preference: {error}", error=exc))
            return False

        vault_cfg.macro_migration_prompt_enabled = bool(enabled)
        return True

    def _load_macro_migration_state() -> dict[str, object] | None:
        """Load migration readiness state for printer.cfg -> macros.cfg flow."""
        try:
            return service.get_macro_migration_state()
        except Exception as exc:
            state.status_label.set_text(t("Failed to inspect macro migration state: {error}", error=exc))
            return None

    def _refresh_macro_migration_action_visibility() -> None:
        """Show manual migration menu action only when migration can still run."""
        target_element = macro_migration_menu_item_wrapper or macro_migration_menu_item
        if target_element is None:
            return
        migration_state = _load_macro_migration_state()
        visible = False
        if migration_state is not None:
            visible = (
                state.current_view == "macro"
                and bool(migration_state.get("can_migrate", False))
            )
        target_element.set_visibility(visible)

    def _refresh_reload_buttons() -> None:
        """Show exactly one pending reload action button when printer is idle."""
        show_restart, show_dynamic_reload = _reload_button_state(
            printer_is_printing=state.printer_is_printing,
            printer_is_busy=state.printer_is_busy,
            restart_required=state.restart_required,
            dynamic_reload_required=state.dynamic_reload_required,
        )
        in_macro_view = state.current_view == "macro"

        if state.restart_klipper_button:
            state.restart_klipper_button.set_enabled(show_restart and in_macro_view)
            state.restart_klipper_button.set_visibility(show_restart and in_macro_view)

        if state.reload_dynamic_macros_button:
            state.reload_dynamic_macros_button.set_enabled(show_dynamic_reload and in_macro_view)
            state.reload_dynamic_macros_button.set_visibility(show_dynamic_reload and in_macro_view)

    def _refresh_save_config_button() -> None:
        """Enable Save Config only when local changes are pending and printer is idle."""
        if state.save_config_button is None:
            return
        is_ready = _remote_actions_available()
        enabled = _save_config_button_enabled(
            is_ready=is_ready,
            printer_is_printing=state.printer_is_printing,
            has_unsynced_local_changes=state.has_unsynced_local_changes,
            is_virtual_printer=_active_printer_is_virtual(),
        )
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
        standard_ready = (not standard_mode_enabled) or state.standard_profile_ready
        return standard_ready

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

    def _set_standard_profile_state(ready: bool, detail: str = "") -> None:
        """Update standard profile status indicators."""
        state.standard_profile_ready = ready
        label = state.standard_profile_label
        if label is None:
            return
        status_text, label_class = _standard_profile_status(ready, detail)
        state.standard_profile_status_text = status_text
        label.classes(replace=label_class)
        label.set_text(state.standard_profile_status_text)
        if state.standard_cfg_list_button is not None:
            state.standard_cfg_list_button.set_visibility(standard_mode_enabled and (not _active_printer_is_virtual()))
        _refresh_save_config_button()

    def refresh_standard_profile_state() -> None:
        """Refresh standard profile readiness state from local profile storage."""
        if not standard_mode_enabled:
            return
        was_ready = state.standard_profile_ready
        active_printer = service.get_active_printer_profile()
        if isinstance(active_printer, dict) and bool(active_printer.get("is_virtual", False)):
            profile_name = str(active_printer.get("profile_name", "")).strip() or t("unnamed")
            _set_standard_profile_state(True, t("{profile} (virtual local-only)", profile=profile_name))
            if not was_ready and state.standard_profile_ready:
                _maybe_run_deferred_startup_scan("virtual printer became ready")
            return

        try:
            profile = service.get_active_ssh_profile()
        except Exception as exc:
            _set_standard_profile_state(False, str(exc))
            return

        if not isinstance(profile, dict) or not profile:
            _set_standard_profile_state(False)
            return

        profile_name = str(profile.get("profile_name", "")).strip() or t("unnamed")
        auth_mode = str(profile.get("auth_mode", "")).strip().lower()
        has_secret = bool(profile.get("has_secret", False))
        if auth_mode in {"password", "key"} and not has_secret:
            _set_standard_profile_state(False, t("{profile} (missing credentials)", profile=profile_name))
            return

        _set_standard_profile_state(True, profile_name)
        if state.printer_state == "unknown" and state.standard_profile_label is not None:
            detail = str(state.printer_status_message or "").strip()
            state.standard_profile_label.classes(replace="text-xs text-negative")
            state.standard_profile_label.set_text(_printer_offline_status_text(detail))
        if not was_ready and state.standard_profile_ready:
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

    def _files_include_dynamic_macros(paths: list[str]) -> bool:
        """Return True when any touched cfg path maps to known dynamic macros."""
        if not paths:
            return False

        dynamic_files = _dynamic_macro_file_paths(state.cached_macros)
        if not dynamic_files:
            return False

        normalized = _normalize_touched_cfg_paths(paths, config_dir)
        return _paths_include_dynamic_macros(normalized, dynamic_files)

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
        """Refresh printer profile metadata used by start-page cards."""
        state.printer_profile_option_ids.clear()
        try:
            profiles = service.list_printer_profiles()
        except Exception as exc:
            state.status_label.set_text(t("Failed to load printer profiles: {error}", error=exc))
            return

        has_non_default_profile = any(
            isinstance(raw, dict) and str(raw.get("profile_name", "")).strip() and str(raw.get("profile_name", "")).strip() != "Default Printer"
            for raw in profiles
        )

        selected_profile_id = 0
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
            state.printer_profile_option_ids[option] = profile_id
            if bool(raw.get("is_active", False)):
                selected_profile_id = profile_id

        state.selected_printer_profile_id = selected_profile_id
        render_printer_cards()

    def _open_macro_workspace_for_profile(profile_id: int) -> None:
        """Activate selected profile and open the macro workspace view."""
        if profile_id <= 0:
            return
        try:
            result = service.activate_printer_profile(profile_id)
        except Exception as exc:
            message = t("Failed to activate printer profile: {error}", error=exc)
            state.status_label.set_text(message)
            _safe_notify(message, "negative")
            return
        if not bool(result.get("ok", False)):
            message = t("Failed to activate printer profile.")
            state.status_label.set_text(message)
            _safe_notify(message, "warning")
            return

        refresh_printer_profile_selector()
        if standard_mode_enabled:
            refresh_standard_profile_state()
        refresh_print_state()
        refresh_data()
        _set_view("macro")
        _refresh_reload_buttons()
        _refresh_save_config_button()
        _open_macro_migration_prompt_if_needed("workspace_open")
        _maybe_run_deferred_startup_scan("printer profile selected")
        message = t("Active printer profile updated.")
        state.status_label.set_text(message)
        _safe_notify(message, "positive")

    def _printer_card_connection_text(profile: dict[str, object]) -> str:
        """Build a concise connection label for one printer profile card."""
        profile_name = str(profile.get("profile_name", "")).strip() or t("unnamed")
        if bool(profile.get("is_virtual", False)):
            return t("{profile} - virtual local-only", profile=profile_name)
        host = str(profile.get("ssh_host", "")).strip()
        port = _to_int(profile.get("ssh_port"), default=22)
        if host:
            return t("{profile} - {host}:{port}", profile=profile_name, host=host, port=port)
        return t("{profile} - local", profile=profile_name)

    def _show_printer_editor(title_text: str) -> None:
        """Reveal the printer editor card with a contextual title."""
        if state.printer_editor_title is not None:
            state.printer_editor_title.set_text(title_text)
        if state.printer_editor_card is not None:
            state.printer_editor_card.set_visibility(True)

    def _hide_printer_editor() -> None:
        """Hide the printer editor card until explicitly requested."""
        if state.printer_editor_card is not None:
            state.printer_editor_card.set_visibility(False)

    def _open_add_printer_editor() -> None:
        """Open editor in add-printer mode."""
        refresh_ssh_profiles_dialog()
        reset_ssh_profile_form_for_new()
        _show_printer_editor(t("Add printer"))

    def _open_edit_printer_editor(profile_id: int) -> None:
        """Open editor in edit mode for one printer profile."""
        if profile_id <= 0:
            return

        refresh_ssh_profiles_dialog()
        selected_option = ""
        for option_label, option_profile_id in state.ssh_profile_option_ids.items():
            if int(option_profile_id) == int(profile_id):
                selected_option = option_label
                break

        if selected_option:
            state.ssh_profile_select.set_value(selected_option)
            _load_selected_ssh_profile()
        else:
            reset_ssh_profile_form_for_new()
            for profile in service.list_printer_profiles():
                if _to_int(profile.get("id"), default=0) != int(profile_id):
                    continue
                state.ssh_profile_name_input.set_value(str(profile.get("profile_name", "") or ""))
                state.ssh_profile_host_input.set_value(str(profile.get("ssh_host", "") or ""))
                state.ssh_profile_port_input.set_value(_to_int(profile.get("ssh_port"), default=22))
                state.ssh_profile_username_input.set_value(str(profile.get("ssh_username", "") or ""))
                state.ssh_profile_remote_dir_input.set_value(str(profile.get("ssh_remote_config_dir", "") or ""))
                state.ssh_profile_moonraker_url_input.set_value(str(profile.get("ssh_moonraker_url", "") or ""))
                auth_mode = str(profile.get("ssh_auth_mode", "key") or "key").strip().lower()
                state.ssh_profile_auth_mode_select.set_value(auth_mode if auth_mode in {"key", "password"} else "key")
                state.ssh_profile_active_toggle.set_value(bool(profile.get("is_active", False)))
                _set_auth_mode_fields()
                break

        _show_printer_editor(t("Edit printer"))

    def _render_printer_cards_impl() -> None:
        """Render selectable printer cards with live status on start page."""
        if state.printer_cards_container is None or state.printer_cards_container.is_deleted:
            return
        state.printer_cards_container.clear()
        try:
            profiles = service.list_printer_profiles()
        except Exception as exc:
            if not state.start_page_status_label.is_deleted:
                state.start_page_status_label.set_text(t("Failed to load printer profiles: {error}", error=exc))
            return

        has_non_default_profile = any(
            isinstance(raw, dict)
            and str(raw.get("profile_name", "")).strip()
            and str(raw.get("profile_name", "")).strip() != "Default Printer"
            for raw in profiles
        )

        card_count = 0
        with state.printer_cards_container:
            for raw in profiles:
                if not isinstance(raw, dict):
                    continue
                profile_id = _to_int(raw.get("id"), default=0)
                if profile_id <= 0:
                    continue
                profile_name = str(raw.get("profile_name", "")).strip() or t("unnamed")
                if has_non_default_profile and profile_name == "Default Printer":
                    continue

                vendor = str(raw.get("vendor", "")).strip() or t("unknown")
                model = str(raw.get("model", "")).strip() or t("unknown")
                active = bool(raw.get("is_active", False))
                is_virtual = bool(raw.get("is_virtual", False))
                status = state.printer_card_status.get(profile_id, {})
                connected = bool(status.get("connected", False))

                with ui.card().classes("w-[22rem] max-w-full"):
                    with ui.row().classes("w-full items-center justify-between"):
                        ui.label(profile_name).classes("text-base font-semibold")
                        if is_virtual:
                            badge_text = t("Virtual")
                            badge_class = "text-xs text-primary"
                        else:
                            badge_text = t("Online") if connected else t("Offline")
                            badge_class = "text-xs text-positive" if connected else "text-xs text-negative"
                        ui.label(badge_text).classes(badge_class)
                    ui.label(t("{vendor} {model}", vendor=vendor, model=model)).classes("text-sm text-grey-4")
                    ui.label(_printer_card_connection_text(raw)).classes("text-xs text-grey-5")
                    if active:
                        ui.label(t("Active profile")).classes("text-xs text-primary")
                    with ui.row().classes("w-full justify-end gap-2 mt-2"):
                        edit_button = ui.button(
                            t("Edit"),
                            icon="edit",
                            on_click=lambda _event, pid=profile_id: _open_edit_printer_editor(pid),
                        ).props("flat dense no-caps")
                        if is_virtual:
                            edit_button.set_visibility(False)
                        
                        def make_delete_click_handler(pid: int):
                            def on_delete_click():
                                profile_name = str([p for p in profiles if p.get("id") == pid][:1][0].get("profile_name", "Unknown")) if profiles else "Unknown"
                                state.printer_delete_profile_id = pid
                                state.printer_delete_dialog_title.set_text(t("Delete printer '{name}'?", name=profile_name))
                                state.printer_delete_error_label.set_text("")
                                state.printer_delete_dialog.open()
                            return on_delete_click
                        
                        ui.button(
                            icon="delete",
                            on_click=make_delete_click_handler(profile_id),
                        ).props("flat dense no-caps").tooltip(t("Delete printer"))
                        
                        ui.button(
                            t("Connect"),
                            icon="arrow_forward",
                            on_click=lambda _event, pid=profile_id: _open_macro_workspace_for_profile(pid),
                        ).props("flat dense no-caps")
                card_count += 1

        if not state.start_page_status_label.is_deleted:
            if card_count <= 0:
                state.start_page_status_label.set_text(t("No printer profiles found. Configure one below."))
            else:
                state.start_page_status_label.set_text(t("Configured printers: {count}", count=card_count))

    render_printer_cards = _render_printer_cards_impl

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
        settings_theme_mode_select.set_value(_normalized_theme_mode(vault_cfg.theme_mode))
        settings_repo_url_input.set_value(str(vault_cfg.online_update_repo_url or "").strip())
        settings_ref_input.set_value(str(vault_cfg.online_update_ref or "").strip())
        settings_developer_toggle.set_value(bool(vault_cfg.developer))
        settings_error_label.set_text("")
        settings_info_label.set_text(
            t(
                "UI language and theme changes apply immediately. Developer mode still requires app restart. "
                "Create PR always uses [vendor]/[model]/manifest.json."
            )
        )
        app_settings_dialog.open()

    def save_app_settings_dialog() -> None:
        """Validate and persist app settings in the SQLite configuration store."""
        version_history_size = _to_int(settings_version_history_input.value, default=0)
        ui_language = str(settings_language_select.value or "").strip().lower()
        theme_mode = _normalized_theme_mode(settings_theme_mode_select.value)
        repo_url = str(settings_repo_url_input.value or "").strip()
        update_ref = str(settings_ref_input.value or "").strip()
        developer_mode = bool(settings_developer_toggle.value)

        if version_history_size < 1:
            settings_error_label.set_text(t("Version history size must be at least 1."))
            return
        if ui_language not in {"en", "de", "fr"}:
            settings_error_label.set_text(t("Unsupported UI language."))
            return
        if theme_mode not in {"auto", "light", "dark"}:
            settings_error_label.set_text(t("Unsupported theme mode."))
            return
        if not update_ref:
            settings_error_label.set_text(t("Online update reference is required."))
            return

        language_changed = str(vault_cfg.ui_language or "en").strip().lower() != ui_language
        restart_required = bool(vault_cfg.developer) != developer_mode

        new_cfg = VaultConfig(
            version_history_size=version_history_size,
            port=10090,
            runtime_mode="standard",
            ui_language=ui_language,
            printer_vendor=str(vault_cfg.printer_vendor or "").strip(),
            printer_model=str(vault_cfg.printer_model or "").strip(),
            online_update_repo_url=repo_url,
            online_update_ref=update_ref,
            theme_mode=theme_mode,
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
        vault_cfg.online_update_ref = str(new_cfg.online_update_ref)
        vault_cfg.theme_mode = str(new_cfg.theme_mode)
        vault_cfg.developer = bool(new_cfg.developer)
        service.set_version_history_size(vault_cfg.version_history_size)
        _apply_theme_mode(dark_mode, vault_cfg.theme_mode)

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

    # ── Section builder: backup dialogs ──────────────────────────────────────
    def _build_backup_dialogs() -> None:
        """Build create-backup and backup-view dialogs; store references on state."""
        nonlocal backup_name_input, backup_error_label, create_backup_button
        nonlocal backup_view_title, backup_view_subtitle, backup_view_table

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

    # ── Build backup dialogs ───────────────────────────────────────────────────
    _build_backup_dialogs()

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

    def _sync_after_remote_conflict() -> None:
        """Close conflict dialog and run a one-click recovery sync/index."""
        remote_conflict_dialog.close()
        asyncio.create_task(perform_index("remote conflict recovery"))

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
            state.status_label.set_text(t("Cannot remove deleted macro: missing identity."))
            return

        try:
            result = service.remove_deleted(file_path, macro_name)
        except Exception as exc:
            state.status_label.set_text(t("Failed to remove deleted macro: {error}", error=exc))
            return

        reason = str(result.get("reason", ""))
        removed = _to_int(result.get("removed", 0))
        if removed > 0:
            state.status_label.set_text(t(
                "Removed deleted macro '{macro_name}' from {file_path} ({removed} row(s)).",
                macro_name=macro_name,
                file_path=file_path,
                removed=removed,
            ))
        elif reason == "not_deleted":
            state.status_label.set_text(t("Selected macro is not marked deleted; nothing removed."))
        elif reason == "not_found":
            state.status_label.set_text(t("Macro not found in database."))
        else:
            state.status_label.set_text(t("No rows removed."))

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
        asyncio.create_task(perform_index("macro restore", sync_remote=False))

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
        asyncio.create_task(perform_index("macro edit", sync_remote=False))

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
        asyncio.create_task(perform_index("macro delete", sync_remote=False))

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
        """Execute confirmed macro deletion from the state.viewer dialog."""
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
        state.duplicates_button.set_text(t("Show all macros") if state.show_duplicates_only else t("Show duplicates"))

    def update_new_button_label() -> None:
        """Sync new-macros filter button text with current filter state."""
        state.new_button.set_text(t("Show all macros") if state.show_new_only else t("Show new"))

    def update_active_filter_button_label() -> None:
        """Sync active/inactive cycle button text with current filter state."""
        state.active_filter_button.set_text(t("Filter: {state}", state=_translated_active_filter_state(state.active_filter)))

    def render_status_badge(status_key: str) -> None:
        """Render a status badge with centralized label/class mapping."""
        ui.label(t(status_key)).classes(_STATUS_BADGE_CLASSES[status_key])

    def on_sort_change(e) -> None:
        """Radio selection change handler for sort order."""
        state.sort_order = e.value
        render_macro_list()

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

    def _current_duplicate_group() -> dict[str, object] | None:
        """Return currently selected duplicate wizard group or None when unavailable."""
        if not state.duplicate_wizard_groups:
            return None
        return state.duplicate_wizard_groups[state.duplicate_wizard_index]

    def _current_duplicate_selection() -> tuple[str, list[dict[str, object]]]:
        """Return current duplicate macro name and entry list."""
        group = _current_duplicate_group() or {}
        macro_name = str(group.get("macro_name", ""))
        entries = _as_dict_list(group.get("entries", []))
        return macro_name, entries

    def _wizard_has_loaded_duplicates() -> bool:
        """Validate duplicate wizard has groups and set user-facing error when missing."""
        if state.duplicate_wizard_groups:
            return True
        state.duplicate_wizard_error.set_text(t("No duplicates loaded."))
        return False

    def _advance_duplicate_wizard(step_delta: int) -> None:
        """Move duplicate wizard index by delta when within bounds and rerender."""
        next_index = state.duplicate_wizard_index + int(step_delta)
        if next_index < 0 or next_index >= len(state.duplicate_wizard_groups):
            return
        state.duplicate_wizard_index = next_index
        _render_duplicate_wizard_step()

    def _validate_duplicate_compare_pair() -> tuple[str, str, str] | None:
        """Return validated duplicate compare tuple (macro, keep, compare) or None with error set."""
        macro_name, _ = _current_duplicate_selection()
        keep_file = str(state.duplicate_keep_choices.get(macro_name, ""))
        compare_file = str(state.duplicate_compare_with_choices.get(macro_name, ""))
        if not keep_file or not compare_file:
            state.duplicate_wizard_error.set_text(t("Select two definitions to compare."))
            return None
        if keep_file == compare_file:
            state.duplicate_wizard_error.set_text(t("Choose a different definition for comparison."))
            return None
        return macro_name, keep_file, compare_file

    def _render_duplicate_wizard_step() -> None:
        """Render one duplicate macro group in the wizard."""
        if not _wizard_has_loaded_duplicates():
            return

        macro_name, entries = _current_duplicate_selection()

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
                        render_status_badge(_status_badge_key(entry))

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
        if not _wizard_has_loaded_duplicates():
            return
        macro_name, entries = _current_duplicate_selection()
        keep_file = str(e.value or "")
        state.duplicate_keep_choices[macro_name] = keep_file
        _update_duplicate_compare_choice(entries, keep_file)

    def _on_duplicate_compare_with_change(e) -> None:
        """Persist selected compare target for current duplicate group."""
        if not _wizard_has_loaded_duplicates():
            return
        macro_name, _ = _current_duplicate_selection()
        state.duplicate_compare_with_choices[macro_name] = str(e.value or "")

    def open_duplicate_pair_compare() -> None:
        """Open side-by-side compare view for currently selected duplicate pair."""
        if not _wizard_has_loaded_duplicates():
            return

        selection = _validate_duplicate_compare_pair()
        if selection is None:
            return
        macro_name, keep_file, compare_file = selection

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
        _advance_duplicate_wizard(-1)

    def duplicate_wizard_next() -> None:
        """Navigate to next duplicate group."""
        _advance_duplicate_wizard(1)

    def apply_duplicate_resolution() -> None:
        """Apply keep choices by deleting duplicate sections from cfg files."""
        if not _wizard_has_loaded_duplicates():
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
        asyncio.create_task(perform_index("duplicate wizard", sync_remote=False))

    def render_macro_list() -> None:
        """Render the left macro list with filters, badges, and selection state."""
        if not _ui_still_available():
            return
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
        state.macro_count_label.set_text(
            t("Items: {visible} / {total}", visible=len(visible_macros), total=state.total_macro_rows)
            if filter_active
            else t("Items: {visible}", visible=len(visible_macros))
        )

        total_pages = max(1, (max(state.total_macro_rows, 1) + state.list_page_size - 1) // state.list_page_size)
        state.prev_page_button.set_enabled(state.list_page_index > 0)
        state.next_page_button.set_enabled((state.list_page_index + 1) < total_pages)

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
                    render_status_badge(_status_badge_key(macro))

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
        if not _ui_still_available():
            return
        state.backup_list.clear()
        backups = service.list_backups()
        if not backups:
            with state.backup_list:
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
            restore_message = t(
                "Restore backup '{backup_name}'? This replaces the current indexed macro state.",
                backup_name=state.restore_target_name,
            )
            if state.restore_target_id is not None:
                try:
                    restore_policy = service.get_backup_restore_policy(int(state.restore_target_id))
                except Exception:
                    restore_policy = {}
                if bool(restore_policy.get("will_overwrite_printer_cfg", False)):
                    warning_message = t(
                        "This backup will overwrite your printer.cfg, make sure your calibration is correct before pinting"
                    )
                    restore_message = f"{restore_message}\n\n{warning_message}\n\n{t('Abort to cancel or Continue to apply this restore.')}"
                    confirm_restore_button.set_text(t("Continue"))
                else:
                    confirm_restore_button.set_text(t("Restore"))
            else:
                confirm_restore_button.set_text(t("Restore"))
            state.restore_confirm_label.set_text(restore_message)
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

        with state.backup_list:
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
        restore_target_id = _selected_restore_target_id()
        if restore_target_id is None:
            return

        asyncio.create_task(_perform_restore_async(restore_target_id))

    async def _perform_restore_async(restore_target_id: int) -> None:
        """Restore backup and immediately sync restored cfg state to the active printer when possible."""
        if state.printer_is_printing:
            state.restore_error_label.set_text(t("Blocked while printer is printing."))
            state.status_label.set_text(t("Blocked: printer is currently printing. Restore is disabled."))
            return

        try:
            result = await asyncio.to_thread(service.restore_backup, restore_target_id)
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
        uploaded_after_restore = False
        if standard_mode_enabled and state.standard_profile_ready:
            try:
                save_result = await state._run_with_file_operation_modal(
                    t("Uploading restored cfg files to printer"),
                    lambda: service.save_config_to_remote(
                        allow_protected_upload=True,
                        progress_callback=state._set_file_operation_progress,
                    ),
                )
            except Exception as exc:
                _set_restore_status_from_result(result)
                state.status_label.set_text(
                    t(
                        "{status} Restore completed locally but upload failed: {error}. Use Save Config to retry upload.",
                        status=state.status_label.text,
                        error=exc,
                    )
                )
                _mark_local_changes_pending()
            else:
                uploaded_after_restore = True
                _mark_local_changes_saved()
                _append_restart_policy_from_result(save_result)
                state.status_label.set_text(
                    t(
                        "Restored backup '{backup_name}' at {restored_at} with {macro_count} macro(s); rewrote {cfg_file_count} cfg file(s). Uploaded {uploaded} and removed {removed} remote cfg file(s).",
                        backup_name=result["backup_name"],
                        restored_at=_format_ts(_to_int(result.get("restored_at", 0))),
                        macro_count=result["macro_count"],
                        cfg_file_count=_to_int(result.get("restored_cfg_files", 0), default=0),
                        uploaded=_to_int(save_result.get("uploaded_files", 0), default=0),
                        removed=_to_int(save_result.get("removed_remote_files", 0), default=0),
                    )
                )
        else:
            _set_restore_status_from_result(result)
            _mark_local_changes_pending()

        if not uploaded_after_restore and (not standard_mode_enabled or not state.standard_profile_ready):
            _mark_local_changes_pending()

        _mark_reload_required(is_dynamic=False)
        asyncio.create_task(perform_index("backup restore", sync_remote=False))

    def perform_delete_backup() -> None:
        """Delete selected backup and refresh the backup list."""
        if blocked_by_print_state(
            status_message="Blocked: printer is currently printing. Delete is disabled.",
            local_error_label=state.delete_error_label,
        ):
            return
        delete_target_id = _selected_delete_target_id()
        if delete_target_id is None:
            return

        try:
            result = service.delete_backup(delete_target_id)
        except Exception as exc:
            state.delete_error_label.set_text(t("Delete failed: {error}", error=exc))
            state.status_label.set_text(t("Delete failed: {error}", error=exc))
            return

        state.delete_dialog.close()
        state.status_label.set_text(t("Deleted backup '{backup_name}'.", backup_name=result["backup_name"]))
        render_backup_list()

    def _selected_restore_target_id() -> int | None:
        """Return selected restore target id or set local error when none is selected."""
        if state.restore_target_id is None:
            state.restore_error_label.set_text(t("No backup selected."))
            return None
        return int(state.restore_target_id)

    def _selected_delete_target_id() -> int | None:
        """Return selected delete target id or set local error when none is selected."""
        if state.delete_target_id is None:
            state.delete_error_label.set_text(t("No backup selected."))
            return None
        return int(state.delete_target_id)

    def _set_restore_status_from_result(result: dict[str, object]) -> None:
        """Render restore completion status summary from restore result payload."""
        restored_label = _format_ts(_to_int(result.get("restored_at", 0)))
        rewritten = _to_int(result.get("restored_cfg_files", 0))
        if rewritten > 0:
            state.status_label.set_text(
                t(
                    "Restored backup '{backup_name}' at {restored_at} with {macro_count} macro(s); rewrote {cfg_file_count} cfg file(s).",
                    backup_name=result["backup_name"],
                    restored_at=restored_label,
                    macro_count=result["macro_count"],
                    cfg_file_count=rewritten,
                )
            )
            return

        state.status_label.set_text(
            t(
                "Restored backup '{backup_name}' at {restored_at} with {macro_count} macro(s). "
                "No cfg snapshot was stored in this backup; only DB state was restored.",
                backup_name=result["backup_name"],
                restored_at=restored_label,
                macro_count=result["macro_count"],
            )
        )

    def refresh_data() -> None:
        """Reload all list/stats data from SQLite and rerender UI sections."""
        if not _ui_still_available():
            return
        _note_activity()
        loaded = _load_dashboard_page()
        if loaded is None:
            return

        stats, _ = loaded
        if not _normalize_dashboard_page_bounds(stats):
            return

        _apply_dashboard_stats(stats)

        render_macro_list()
        render_backup_list()

    def _load_dashboard_page() -> tuple[dict[str, object], list[dict[str, object]]] | None:
        """Load one dashboard page and update cached rows/state total counters."""
        try:
            stats, rows = service.load_dashboard(
                limit=state.list_page_size,
                offset=state.list_page_index * state.list_page_size,
            )
        except Exception as exc:
            state.status_label.set_text(t("Data refresh failed: {error}", error=exc))
            return None
        state.cached_macros = rows
        state.total_macro_rows = _to_int(stats.get("total_macros", len(rows)), default=len(rows))
        return stats, rows

    def _normalize_dashboard_page_bounds(stats: dict[str, object]) -> bool:
        """Clamp page index to valid bounds and reload when current page overflows."""
        total_pages = max(1, (max(state.total_macro_rows, 1) + state.list_page_size - 1) // state.list_page_size)
        if state.list_page_index < total_pages:
            return True

        state.list_page_index = max(0, total_pages - 1)
        reloaded = _load_dashboard_page()
        if reloaded is None:
            return False
        reloaded_stats, _ = reloaded
        stats.clear()
        stats.update(reloaded_stats)
        return True

    def _apply_dashboard_stats(stats: dict[str, object]) -> None:
        """Apply dashboard aggregate stats and duplicate state to UI labels/flags."""
        deleted_macros = _to_int(stats.get("deleted_macros", 0))
        state.deleted_macro_count = deleted_macros
        state.cached_duplicate_names = duplicate_names_for_macros(state.cached_macros)
        duplicate_groups = service.list_duplicates()
        duplicate_macros = len(duplicate_groups)
        state._cached_versions_key = None
        state._cached_versions = []
        state.duplicate_warning_button.set_visibility((duplicate_macros > 0) and state.current_view == "macro")
        state.total_macros_label.set_text(t("Total macros: {count}", count=stats["total_macros"]))
        state.duplicate_macros_label.set_text(t("Duplicate macros: {count}", count=duplicate_macros))
        state.deleted_macros_label.set_text(t("Deleted macros: {count}", count=deleted_macros))
        state.purge_deleted_button.set_visibility(deleted_macros > 0)
        state.distinct_files_label.set_text(t("Config files: {count}", count=stats["distinct_cfg_files"]))
        state.last_update_label.set_text(t("Last update: {value}", value=_format_ts(_to_optional_int(stats.get("latest_update_ts")))))
        _refresh_macro_migration_action_visibility()

    async def perform_index(trigger: str, *, sync_remote: bool = True) -> None:
        """Run cfg indexing and refresh UI when complete."""
        if not _ui_still_available():
            return
        if not _can_start_indexing():
            return
        _note_activity()
        state.is_indexing = True
        try:
            state.status_label.set_text(t("Scanning macros ({trigger})...", trigger=trigger))
            result = await state._run_with_file_operation_modal(
                t("Scanning and parsing cfg files"),
                lambda: service.index(progress_callback=state._set_file_operation_progress, sync_remote=sync_remote),
            )
            if not _ui_still_available():
                return
            _apply_index_result(result=result, trigger=trigger)
        except FileNotFoundError as exc:
            state.status_label.set_text(t("Error: {error}", error=exc))
        except Exception as exc:
            state.status_label.set_text(t("Scan failed: {error}", error=exc))
        finally:
            state.is_indexing = False

    def _can_start_indexing() -> bool:
        """Validate index preconditions and show guidance when unavailable."""
        if state.is_indexing:
            return False
        if standard_mode_enabled:
            refresh_standard_profile_state()
        if standard_mode_enabled and not state.standard_profile_ready:
            message = t("Cannot scan macros: configure and activate a printer connection first.")
            state.status_label.set_text(message)
            ui.notify(message, type="warning")
            return False
        return True

    def _index_status_text(result: dict[str, object]) -> str:
        """Build user-facing index completion summary including optional remote sync stats."""
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
        return status_text

    def _prompt_for_missing_printer_identity() -> None:
        """Open printer profile dialog when active profile has incomplete identity fields."""
        active_profile = service.get_active_printer_profile()
        if not isinstance(active_profile, dict):
            return

        vendor = str(active_profile.get("vendor", "")).strip()
        model = str(active_profile.get("model", "")).strip()
        if vendor and model:
            return
        state.printer_vendor_input.set_value(vendor)
        state.printer_model_input.set_value(model)
        state.printer_profile_dialog.open()

    def _apply_index_result(*, result: dict[str, object], trigger: str) -> None:
        """Apply successful indexing result to UI state and follow-up actions."""
        state.status_label.set_text(_index_status_text(result))
        if trigger != "startup" and _to_int(result.get("macros_inserted", 0)) > 0:
            inserted = _to_int(result.get("macros_inserted", 0))
            dynamic_inserted = _to_int(result.get("dynamic_macros_inserted", 0))
            _mark_reload_required(is_dynamic=(inserted > 0 and dynamic_inserted == inserted))

        normalized_trigger = str(trigger or "").strip().lower()
        if normalized_trigger in {"macro migration", "backup restore"}:
            try:
                purge_result = service.purge_all_deleted()
            except Exception as exc:
                state.status_label.set_text(t("Failed to purge deleted macros after operation: {error}", error=exc))
            else:
                purged_count = _to_int(purge_result.get("removed", 0), default=0)
                if purged_count > 0:
                    state.status_label.set_text(
                        t(
                            "{status} Purged {removed} deleted macro row(s).",
                            status=state.status_label.text,
                            removed=purged_count,
                        )
                    )
        refresh_data()
        _prompt_for_missing_printer_identity()

    def _maybe_run_deferred_startup_scan(reason: str) -> None:
        """Run one deferred startup scan once standard prerequisites are ready."""
        if not _can_run_deferred_startup_scan():
            return

        state.deferred_startup_scan = False
        asyncio.create_task(perform_index("startup"))

    def _can_run_deferred_startup_scan() -> bool:
        """Return True when deferred startup scan preconditions are satisfied."""
        if not state.deferred_startup_scan:
            return False
        if state.current_view != "macro":
            return False
        if state.is_indexing or state.printer_is_printing:
            return False
        if standard_mode_enabled and not state.standard_profile_ready:
            return False
        return True

    def open_backup_dialog() -> None:
        """Open backup creation dialog with generated default name."""
        backup_name_input.value = datetime.now().strftime("backup-%Y%m%d-%H%M%S")
        backup_name_input.update()
        state.backup_dialog.open()

    def _dict_rows(raw_rows: object) -> list[dict[str, object]]:
        """Normalize dynamic dashboard rows to a list of dictionaries."""
        if not isinstance(raw_rows, list):
            return []
        return [row for row in raw_rows if isinstance(row, dict)]

    def _load_order_lines(file_rows: list[dict[str, object]], macro_rows: list[dict[str, object]]) -> list[str]:
        """Build textual load-order overview lines for files and macros."""
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
        return lines

    def _set_load_order_summary(overview: dict[str, object], file_rows: list[dict[str, object]], macro_rows: list[dict[str, object]]) -> None:
        """Set load-order summary label from overview payload with row-count fallback."""
        load_order_summary_label.set_text(
            t(
                "Klipper parses {klipper_count} cfg file(s) and {klipper_macro_count} macro section(s).",
                klipper_count=overview.get("klipper_count", len(file_rows)),
                klipper_macro_count=overview.get("klipper_macro_count", len(macro_rows)),
            )
        )

    async def _open_load_order_overview_dialog_async() -> None:
        """Load overview asynchronously and display in dialog with progress modal."""
        try:
            overview = await state._run_with_file_operation_modal(
                t("Loading cfg parsing overview…"),
                lambda: service.load_cfg_loading_overview(),
            )
        except Exception as exc:
            state.status_label.set_text(t("Failed to load cfg parsing overview: {error}", error=exc))
            return

        file_rows = _dict_rows(overview.get("klipper_order", []))
        macro_rows = _dict_rows(overview.get("klipper_macro_order", []))

        _set_load_order_summary(overview, file_rows, macro_rows)
        load_order_text.set_text("\n".join(_load_order_lines(file_rows, macro_rows)))
        state.load_order_dialog.open()

    def open_load_order_overview_dialog() -> None:
        """Open overview dialog asynchronously from UI callbacks."""
        asyncio.create_task(_open_load_order_overview_dialog_async())

    def _validate_backup_name_input() -> str | None:
        """Return sanitized backup name or None when current input is invalid."""
        name = str(backup_name_input.value or "").strip()
        if name:
            return name
        backup_error_label.set_text(t("Please enter a backup name."))
        return None

    def _set_backup_created_status(result: dict[str, object]) -> None:
        """Set backup-created status text from create-backup result payload."""
        created_label = _format_ts(_to_int(result.get("created_at", 0)))
        state.status_label.set_text(
            t(
                "Backup '{backup_name}' created at {created_at} with {macro_count} macro(s) from {cfg_file_count} cfg file(s).",
                backup_name=result["backup_name"],
                created_at=created_label,
                macro_count=result["macro_count"],
                cfg_file_count=result["cfg_file_count"],
            )
        )

    def perform_backup() -> None:
        """Create named backup snapshot and update status/list output."""
        if state.printer_is_printing:
            backup_error_label.set_text(t("Blocked while printer is printing."))
            state.status_label.set_text(t("Blocked: printer is currently printing. Backup is disabled."))
            return
        name = _validate_backup_name_input()
        if name is None:
            return

        asyncio.create_task(_perform_backup_async(name))

    async def _perform_backup_async(name: str) -> None:
        """Run backup in a thread while showing a progress modal."""
        try:
            result = await state._run_with_file_operation_modal(
                t("Creating backup…"),
                lambda: service.create_backup(name, progress_callback=state._set_file_operation_progress),
            )
        except Exception as exc:
            backup_error_label.set_text(t("Backup failed: {error}", error=exc))
            state.status_label.set_text(t("Backup failed: {error}", error=exc))
            return

        state.backup_dialog.close()
        _set_backup_created_status(result)
        render_backup_list()

    def _set_macro_migration_prompt_text(migration_state: dict[str, object]) -> None:
        """Populate migration dialog text from current migration state."""
        printer_cfg_count = _to_int(migration_state.get("printer_cfg_macro_count", 0))
        macros_cfg_count = _to_int(migration_state.get("macros_cfg_macro_count", 0))
        macro_migration_prompt_message.set_text(
            t(
                "Found {printer_count} macro(s) in printer.cfg and {macros_count} in macros.cfg. "
                "Migrate now to move printer.cfg macros into macros.cfg. A backup is created first.",
                printer_count=printer_cfg_count,
                macros_count=macros_cfg_count,
            )
        )

    def _open_macro_migration_prompt_if_needed(trigger: str) -> None:
        """Show one-time migration prompt when entering macro workspace if migration is available."""
        nonlocal _macro_migration_prompt_shown
        normalized_trigger = str(trigger).strip().lower()
        if normalized_trigger != "workspace_open":
            return
        if state.current_view != "macro":
            return
        if _macro_migration_prompt_shown:
            return
        if not bool(vault_cfg.macro_migration_prompt_enabled):
            return

        migration_state = _load_macro_migration_state()
        if migration_state is None:
            return
        if not bool(migration_state.get("can_migrate", False)):
            return

        _macro_migration_prompt_shown = True
        macro_migration_prompt_error.set_text("")
        _set_macro_migration_prompt_text(migration_state)
        macro_migration_prompt_dialog.open()

    def _decline_macro_migration_prompt() -> None:
        """Persist migration prompt opt-out and close migration prompt dialog."""
        if not _persist_macro_migration_prompt_enabled(False):
            return
        macro_migration_prompt_dialog.close()
        state.status_label.set_text(t("Macro migration prompt disabled. Use Macro actions to migrate later."))
        _refresh_macro_migration_action_visibility()

    async def _perform_macro_migration_async() -> None:
        """Create backup, migrate macros, and upload resulting cfg changes to printer."""
        if state.printer_is_printing:
            state.status_label.set_text(t("Blocked: printer is currently printing. Macro migration is disabled."))
            return

        try:
            result = await asyncio.to_thread(service.migrate_printer_cfg_macros)
        except Exception as exc:
            macro_migration_prompt_error.set_text(t("Macro migration failed: {error}", error=exc))
            state.status_label.set_text(t("Macro migration failed: {error}", error=exc))
            return

        if macro_migration_prompt_dialog is not None:
            macro_migration_prompt_dialog.close()
        moved_sections = _to_int(result.get("moved_sections", 0), default=0)
        backup_name = str(result.get("backup_name", "")).strip() or "-"
        state.status_label.set_text(
            t(
                "Migrated {count} macro section(s) from printer.cfg to macros.cfg after creating backup '{backup_name}'.",
                count=moved_sections,
                backup_name=backup_name,
            )
        )
        _mark_local_changes_pending()
        _mark_reload_required(is_dynamic=False)

        if standard_mode_enabled and state.standard_profile_ready:
            try:
                save_result = await state._run_with_file_operation_modal(
                    t("Uploading migrated cfg files to printer"),
                    lambda: service.save_config_to_remote(
                        allow_protected_upload=True,
                        progress_callback=state._set_file_operation_progress,
                    ),
                )
            except Exception as exc:
                state.status_label.set_text(
                    t(
                        "Migration completed locally but upload failed: {error}. Use Save Config to retry upload.",
                        error=exc,
                    )
                )
            else:
                _mark_local_changes_saved()
                _append_restart_policy_from_result(save_result)
                state.status_label.set_text(
                    t(
                        "Migrated macros and uploaded config to printer: {uploaded} uploaded, {removed} removed, backup '{backup_name}'.",
                        uploaded=_to_int(save_result.get("uploaded_files", 0), default=0),
                        removed=_to_int(save_result.get("removed_remote_files", 0), default=0),
                        backup_name=backup_name,
                    )
                )

        render_backup_list()
        _refresh_macro_migration_action_visibility()
        asyncio.create_task(perform_index("macro migration", sync_remote=False))

    def _perform_macro_migration() -> None:
        """Run migration workflow asynchronously from UI callbacks."""
        asyncio.create_task(_perform_macro_migration_async())

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

    def _selected_export_identities() -> list[tuple[str, str]]:
        """Return selected export macro identities from export dialog checkboxes."""
        selections = [
            identity
            for identity, checkbox in export_macro_checkboxes.items()
            if bool(getattr(checkbox, "value", False))
        ]
        identities: list[tuple[str, str]] = []
        for identity in selections:
            if "::" not in identity:
                continue
            file_path, macro_name = identity.split("::", 1)
            identities.append((file_path, macro_name))
        return identities

    def _set_import_status_from_result(result: dict[str, object]) -> None:
        """Set import completion status text from service result payload."""
        imported = _to_int(result.get("imported", 0))
        source_vendor = str(result.get("source_vendor", "")).strip()
        source_model = str(result.get("source_model", "")).strip()
        if imported <= 0:
            state.status_label.set_text(t("No macros were imported."))
            return

        if bool(result.get("printer_matches", False)):
            state.status_label.set_text(t("Imported {count} macro(s) as new inactive entries.", count=imported))
            return

        if source_vendor and source_model:
            state.status_label.set_text(
                t(
                    "Imported {count} macro(s) for printer {vendor} {model}. Review before enabling.",
                    count=imported,
                    vendor=source_vendor,
                    model=source_model,
                )
            )
            return

        state.status_label.set_text(
            t(
                "Imported {count} macro(s) with unknown source printer. Review before enabling.",
                count=imported,
            )
        )

    def _write_uploaded_import_tempfile(suffix: str) -> Path:
        """Write uploaded import bytes to a temporary file and return its path."""
        temp_import_file = Path(tempfile.gettempdir()) / (
            datetime.now().strftime("klippervault-import-%Y%m%d-%H%M%S") + suffix
        )
        temp_import_file.write_bytes(state.uploaded_import_bytes or b"")
        return temp_import_file

    def _write_uploaded_cfg_import_tempfile() -> Path:
        """Write uploaded cfg import bytes to a temporary .cfg file and return its path."""
        temp_import_file = Path(tempfile.gettempdir()) / (
            datetime.now().strftime("klippervault-import-cfg-%Y%m%d-%H%M%S") + ".cfg"
        )
        temp_import_file.write_bytes(state.uploaded_cfg_import_bytes or b"")
        return temp_import_file

    def perform_export() -> None:
        """Export selected macros to a share file on disk."""
        source_vendor, source_model = _active_printer_identity()
        identities = _selected_export_identities()

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
            state.status_label.set_text(t("Export failed: {error}", error=exc))
            return

        state.export_dialog.close()
        exported_path = Path(str(result.get("file_path", "")))
        _deliver_exported_file(exported_path)
        state.status_label.set_text(
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
        temp_import_file = _write_uploaded_import_tempfile(suffix)

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
        _set_import_status_from_result(result)

        refresh_data()

    def open_import_cfg_dialog() -> None:
        """Open cfg import dialog for virtual-printer local workspace imports."""
        if not _active_printer_is_virtual():
            state.status_label.set_text(t("Import macro.cfg is available only for virtual printers."))
            return
        state.uploaded_cfg_import_bytes = None
        state.uploaded_cfg_import_name = ""
        state.import_cfg_uploader.reset()
        state.import_cfg_error_label.set_text("")
        state.import_cfg_dialog.open()

    def perform_import_cfg() -> None:
        """Import one local cfg file into active virtual-printer runtime workspace."""
        if not _active_printer_is_virtual():
            state.import_cfg_error_label.set_text(t("Import macro.cfg is available only for virtual printers."))
            return

        if not state.uploaded_cfg_import_bytes:
            state.import_cfg_error_label.set_text(t("Please upload a .cfg file."))
            return

        uploaded_name = Path(state.uploaded_cfg_import_name or "").name
        target_name = uploaded_name if uploaded_name.lower().endswith(".cfg") else "macro.cfg"
        temp_import_file = _write_uploaded_cfg_import_tempfile()

        try:
            result = service.import_cfg_file_to_runtime(
                import_file=temp_import_file,
                target_rel_path=target_name,
            )
        except Exception as exc:
            state.import_cfg_error_label.set_text(t("Import cfg failed: {error}", error=exc))
            state.status_label.set_text(t("Import cfg failed: {error}", error=exc))
            return
        finally:
            temp_import_file.unlink(missing_ok=True)

        state.import_cfg_dialog.close()
        _mark_local_changes_pending()
        state.status_label.set_text(
            t(
                "Imported cfg file {path} into local virtual workspace.",
                path=str(result.get("imported_path", "")),
            )
        )
        asyncio.create_task(perform_index("cfg import", sync_remote=False))

    def export_online_update_repo_zip() -> None:
        """Export active local macros as a ZIP for the online update repository."""
        if _printer_profile_missing():
            state.status_label.set_text(t("Set printer vendor/model before exporting update repository zip."))
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
            )
        except Exception as exc:
            state.status_label.set_text(t("Update repository export failed: {error}", error=exc))
            return

        exported_path = Path(str(result.get("file_path", "")))
        _deliver_exported_file(exported_path)
        state.status_label.set_text(
            t(
                "Exported {count} active macro(s) as update repository ZIP: {path}",
                count=result.get("macro_count", 0),
                path=result.get("file_path", ""),
            )
        )

    def _refresh_create_pr_progress_ui() -> None:
        """Sync pull-request progress widgets with current background state."""
        if not state.create_pr_in_progress:
            create_pr_progress_label.set_visibility(False)
            create_pr_progress_bar.set_visibility(False)
            return

        progress_value, percent = _progress_value_and_percent(
            state.create_pr_progress_current,
            state.create_pr_progress_total,
        )
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
        state.pr_head_branch_input.set_value(_default_pr_head_branch(source_vendor, source_model))
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

    def open_create_virtual_printer_dialog() -> None:
        """Open developer dialog to create and activate one virtual printer profile."""
        active_profile = service.get_active_printer_profile()
        default_vendor = str(active_profile.get("vendor", "")).strip() if isinstance(active_profile, dict) else ""
        default_model = str(active_profile.get("model", "")).strip() if isinstance(active_profile, dict) else ""
        default_name = (
            t("Virtual {vendor} {model}", vendor=default_vendor or t("Printer"), model=default_model or t("Model"))
            if (default_vendor or default_model)
            else t("Virtual Printer")
        )

        virtual_printer_name_input.set_value(default_name)
        virtual_printer_vendor_input.set_value(default_vendor)
        virtual_printer_model_input.set_value(default_model)
        create_virtual_printer_error_label.set_text("")
        confirm_create_virtual_printer_button.set_enabled(True)
        create_virtual_printer_dialog.open()

    def perform_create_virtual_printer() -> None:
        """Create and auto-activate a developer virtual printer profile."""
        profile_name = str(virtual_printer_name_input.value or "").strip()
        vendor = str(virtual_printer_vendor_input.value or "").strip()
        model = str(virtual_printer_model_input.value or "").strip()

        if not profile_name or not vendor or not model:
            create_virtual_printer_error_label.set_text(t("Profile name, vendor, and model are required."))
            return

        confirm_create_virtual_printer_button.set_enabled(False)
        try:
            result = service.create_virtual_printer_profile(
                profile_name=profile_name,
                vendor=vendor,
                model=model,
                activate=True,
            )
        except Exception as exc:
            create_virtual_printer_error_label.set_text(t("Failed to create virtual printer: {error}", error=exc))
            confirm_create_virtual_printer_button.set_enabled(True)
            return

        if not bool(result.get("ok", False)):
            create_virtual_printer_error_label.set_text(t("Failed to create virtual printer profile."))
            confirm_create_virtual_printer_button.set_enabled(True)
            return

        create_virtual_printer_dialog.close()
        refresh_printer_profile_selector()
        refresh_standard_profile_state()
        refresh_print_state()
        refresh_printer_card_statuses()
        refresh_data()
        _refresh_reload_buttons()
        _refresh_save_config_button()
        message = t("Virtual printer profile created and activated.")
        state.status_label.set_text(message)
        _safe_notify(message, "positive")

    async def perform_create_pr() -> None:
        """Create a pull request on GitHub for current active macro artifacts."""
        source_vendor, source_model = _active_printer_identity()

        inputs = _collect_create_pr_inputs(state)
        validation_error = _validate_create_pr_inputs(
            printer_profile_missing=_printer_profile_missing(),
            inputs=inputs,
        )
        if validation_error:
            state.create_pr_error_label.set_text(validation_error)
            return

        _begin_create_pr_request(state, _refresh_create_pr_progress_ui)
        await asyncio.sleep(0)

        def report_progress(current: int, total: int) -> None:
            state.create_pr_progress_current = max(int(current), 0)
            state.create_pr_progress_total = max(int(total), 1)

        try:
            result = await asyncio.to_thread(
                service.create_online_update_pull_request,
                source_vendor=source_vendor,
                source_model=source_model,
                repo_url=str(inputs.get("repo_url", "")),
                base_branch=str(inputs.get("base_branch", "")),
                head_branch=str(inputs.get("head_branch", "")),
                github_token=str(inputs.get("token", "")),
                pull_request_title=str(inputs.get("title", "")),
                pull_request_body=str(inputs.get("body", "")),
                progress_callback=report_progress,
            )
        except Exception as exc:
            _set_create_pr_request_failure(state, _refresh_create_pr_progress_ui, exc)
            return

        _finish_create_pr_request(state, _refresh_create_pr_progress_ui, result)

    def _refresh_online_update_progress_ui() -> None:
        """Sync online update progress widgets with current background state."""
        if not state.online_update_check_in_progress:
            online_update_progress_label.set_visibility(False)
            online_update_progress_bar.set_visibility(False)
            return

        progress_value, percent = _progress_value_and_percent(
            state.online_update_progress_current,
            state.online_update_progress_total,
        )
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

    def _reset_online_update_dialog_state() -> None:
        """Reset online update dialog widgets and cached pending update state."""
        state.online_update_activate_checkboxes.clear()
        state.online_update_list.clear()
        state.online_update_error_label.set_text("")
        state.online_update_summary_label.set_text("")
        state.pending_online_updates = []
        state.confirm_online_update_button.set_enabled(False)
        state.confirm_online_update_button.set_visibility(False)

    def _validate_online_update_prerequisites() -> str | None:
        """Return configured update repository URL when prerequisites are met, else None."""
        if _printer_profile_missing():
            state.status_label.set_text(t("Set printer vendor/model before checking updates."))
            return None

        repo_url = str(vault_cfg.online_update_repo_url or "").strip()
        if not repo_url:
            state.status_label.set_text(t("Online updater repository URL is not configured."))
            return None
        return repo_url

    def _set_online_update_summary_from_result(result: dict[str, object]) -> None:
        """Set summary text and pending updates from online update check result."""
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

    def _render_online_update_candidates() -> None:
        """Render update checkbox rows and confirm button state."""
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

    def _selected_online_update_activate_identities() -> list[str]:
        """Return identities checked for activation during online update import."""
        return [
            identity
            for identity, checkbox in state.online_update_activate_checkboxes.items()
            if bool(getattr(checkbox, "value", False))
        ]

    async def open_online_update_dialog() -> None:
        """Check online source for changed macros and open update selection dialog."""
        source_vendor, source_model = _active_printer_identity()

        _reset_online_update_dialog_state()
        repo_url = _validate_online_update_prerequisites()
        if repo_url is None:
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

        _set_online_update_summary_from_result(result)
        _render_online_update_candidates()
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

        activate_identities = _selected_online_update_activate_identities()

        try:
            result = service.import_online_updates(
                updates=state.pending_online_updates,
                activate_identities=activate_identities,
                repo_url=vault_cfg.online_update_repo_url,
                repo_ref=vault_cfg.online_update_ref,
            )
        except Exception as exc:
            online_update_error_label.set_text(t("Import updates failed: {error}", error=exc))
            state.status_label.set_text(t("Import updates failed: {error}", error=exc))
            return

        imported = _to_int(result.get("imported", 0))
        activated = _to_int(result.get("activated", 0))
        if imported <= 0:
            online_update_error_label.set_text(t("No online updates were imported."))
            state.status_label.set_text(t("No online updates were imported."))
            return

        online_update_dialog.close()
        state.status_label.set_text(
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
            asyncio.create_task(perform_index("online updates", sync_remote=False))
        else:
            refresh_data()

    def purge_deleted_macros() -> None:
        """Remove all deleted macro histories from SQLite in one action."""
        if state.printer_is_printing:
            state.status_label.set_text(t("Blocked: printer is currently printing. Purge is disabled."))
            return
        try:
            result = service.purge_all_deleted()
        except Exception as exc:
            state.status_label.set_text(t("Failed to purge deleted macros: {error}", error=exc))
            return

        removed = _to_int(result.get("removed", 0))
        if removed > 0:
            state.status_label.set_text(t("Purged {removed} deleted macro row(s) from database.", removed=removed))
        else:
            state.status_label.set_text(t("No deleted macros to purge."))
        refresh_data()

    def run_index() -> None:
        """Manual scan button handler."""
        asyncio.create_task(perform_index("manual"))

    def _run_printer_runtime_command(
        *,
        command,
        failure_template: str,
        success_message: str,
    ) -> bool:
        """Execute a runtime printer command and report status."""
        try:
            command()
        except Exception as exc:
            state.status_label.set_text(t(failure_template, error=exc))
            return False

        _clear_restart_required()
        state.status_label.set_text(t(success_message))
        return True

    def restart_klipper() -> None:
        """Request Klipper restart when macro changes are pending and printer is idle."""
        if not state.restart_required:
            state.status_label.set_text(t("No pending macro changes require a Klipper restart."))
            return
        if state.printer_is_printing or state.printer_is_busy:
            state.status_label.set_text(t("Blocked: printer is busy or printing. Klipper restart is disabled."))
            return

        asyncio.create_task(_restart_klipper_async())

    async def _restart_klipper_async() -> None:
        """Run Klipper restart off the event loop to keep the UI responsive."""
        nonlocal _klipper_restart_grace_until
        state.status_label.set_text(t("Klipper restart requested…"))
        try:
            await asyncio.to_thread(service.restart_klipper, 5.0)
        except Exception as exc:
            state.status_label.set_text(t("Failed to restart Klipper: {error}", error=exc))
            return

        # Grant a grace period so the status poller doesn't immediately show
        # the reconnecting modal while Klipper is coming back up.
        _klipper_restart_grace_until = time.monotonic() + 60.0
        _clear_restart_required()
        state.status_label.set_text(t("Klipper restart requested. The restart button will reappear after another macro change."))

    def reload_dynamic_macros() -> None:
        """Request dynamic macro reload when pending dynamic macro changes exist."""
        if not state.dynamic_reload_required:
            state.status_label.set_text(t("No pending dynamic macro changes require a dynamic macro reload."))
            return

        _run_printer_runtime_command(
            command=lambda: service.reload_dynamic_macros(timeout=3.0),
            failure_template="Failed to reload dynamic macros: {error}",
            success_message=(
                "Dynamic macro reload requested. "
                "The reload button will reappear after another dynamic macro change."
            ),
        )

    def _can_save_config_to_printer() -> bool:
        """Validate Save Config preconditions and set user-facing status on failure."""
        if not standard_mode_enabled:
            state.status_label.set_text(t("Save Config is only available in standard mode."))
            return False
        if _active_printer_is_virtual():
            state.status_label.set_text(t("Virtual printer profile is local-only. Remote Save Config upload is disabled."))
            return False
        if not state.standard_profile_ready:
            state.status_label.set_text(t("Cannot save config: configure and activate a printer connection first."))
            return False
        if state.printer_is_printing:
            state.status_label.set_text(t("Blocked: printer is currently printing. Save Config is disabled."))
            _refresh_save_config_button()
            return False
        if not state.has_unsynced_local_changes:
            state.status_label.set_text(t("No local config changes pending upload."))
            _refresh_save_config_button()
            return False
        return True

    def _set_save_config_success_status(result: dict[str, object]) -> None:
        """Render Save Config completion summary from result payload."""
        uploaded = _to_int(result.get("uploaded_files", 0), default=0)
        removed = _to_int(result.get("removed_remote_files", 0), default=0)
        blocked = _to_int(result.get("blocked_files", 0), default=0)
        state.status_label.set_text(
            t(
                "Save Config complete: {uploaded} uploaded, {removed} removed, {blocked} blocked.",
                uploaded=uploaded,
                removed=removed,
                blocked=blocked,
            )
        )

    async def save_config_to_printer() -> None:
        """Explicitly upload local cfg changes to printer via SFTP when printer is idle."""
        if not _can_save_config_to_printer():
            return

        try:
            result = await state._run_with_file_operation_modal(
                t("Uploading local cfg files to printer"),
                lambda: service.save_config_to_remote(progress_callback=state._set_file_operation_progress),
            )
        except Exception as exc:
            state.status_label.set_text(t("Save Config failed: {error}", error=exc))
            return

        _set_save_config_success_status(result)
        _mark_local_changes_saved()
        _append_restart_policy_from_result(result)

    def _append_restart_policy_from_result(result: dict[str, object]) -> None:
        """Apply restart/dynamic-reload markers from service result payload."""
        if bool(result.get("restart_required", False)):
            _mark_reload_required(is_dynamic=False)
            return
        if bool(result.get("dynamic_reload_required", False)):
            _mark_reload_required(is_dynamic=True)

    def _set_mutation_controls_enabled(local_actions_enabled: bool) -> None:
        """Apply enabled/disabled state for macro mutation controls."""
        index_button.set_enabled(local_actions_enabled)
        macro_actions_button.set_enabled(local_actions_enabled)
        duplicate_warning_button.set_enabled(local_actions_enabled)
        state.purge_deleted_button.set_enabled(local_actions_enabled and state.deleted_macro_count > 0)
        create_backup_button.set_enabled(local_actions_enabled)
        confirm_export_button.set_enabled(local_actions_enabled)
        confirm_import_button.set_enabled(local_actions_enabled)
        confirm_import_cfg_button.set_enabled(local_actions_enabled and _active_printer_is_virtual())
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
        if standard_mode_enabled:
            state.test_active_printer_button.set_enabled(True)
            state.standard_cfg_list_button.set_enabled(state.standard_profile_ready)
        state.viewer.set_editing_enabled(local_actions_enabled)
        _refresh_reload_buttons()
        _refresh_save_config_button()

    def _set_print_state_status_text(*, locked: bool, moonraker_state: str) -> None:
        """Set status label text based on print lock and Moonraker state."""
        if locked:
            state.status_label.set_text(
                t(
                    "Printing in progress ({state}). Local edits are allowed; Save Config upload is disabled.",
                    state=moonraker_state,
                )
            )
            return

        if standard_mode_enabled and not state.standard_profile_ready:
            state.status_label.set_text(t("Ready (waiting for active printer connection)."))
        elif moonraker_state == "unknown":
            state.status_label.set_text(t("Ready (Moonraker status unknown)."))
        else:
            state.status_label.set_text(t("Ready (printer state: {state}).", state=moonraker_state))
        _maybe_run_deferred_startup_scan("printer became idle")

    def set_print_lock(locked: bool, moonraker_state: str, moonraker_message: str) -> None:
        """Toggle UI mutation lock while printer is actively printing."""
        _prev_printer_state = state.printer_state
        state.printer_is_printing = locked
        state.printer_state = moonraker_state
        state.printer_status_message = str(moonraker_message or "")
        state.printer_is_busy = moonraker_state not in {"standby", "ready", "complete", "cancelled"}
        local_actions_enabled = _remote_actions_available()

        _set_mutation_controls_enabled(local_actions_enabled)

        if standard_mode_enabled and state.standard_profile_ready and state.standard_profile_label is not None:
            detail = str(moonraker_message or "").strip()
            if moonraker_state == "unknown":
                offline_text = t("Printer offline")
                if detail:
                    offline_text = t("Printer offline: {detail}", detail=detail)
                state.standard_profile_label.classes(replace="text-xs text-negative")
                state.standard_profile_label.set_text(offline_text)
            else:
                state.standard_profile_label.classes(replace="text-xs text-positive")
                state.standard_profile_label.set_text(state.standard_profile_status_text)

        _set_print_state_status_text(locked=locked, moonraker_state=moonraker_state)

        # Detect printer coming back online after being unreachable and auto-rescan.
        if moonraker_state != "unknown":
            if (
                _prev_printer_state == "unknown"
                and state.printer_seen_connected
                and not locked
                and not state.is_indexing
                and state.current_view == "macro"
            ):
                asyncio.create_task(perform_index("printer came online"))
            state.printer_seen_connected = True

    def refresh_print_state() -> None:
        """Poll Moonraker printer state and apply UI lock policy."""
        nonlocal _print_state_refresh_inflight
        if standard_mode_enabled and not state.standard_profile_ready:
            state._set_printer_connecting_modal(False)
            set_print_lock(
                locked=False,
                moonraker_state="unknown",
                moonraker_message=t("Standard mode active but no printer connection is ready."),
            )
            return

        if _print_state_refresh_inflight:
            return

        async def _query_print_state_status() -> dict[str, object]:
            """Fetch current printer status payload with UI-connectivity signaling."""
            try:
                status = await asyncio.to_thread(service.query_printer_status, 1.5)
                state._set_printer_connecting_modal(False)
                return {
                    "is_printing": bool(status.get("is_printing", False)),
                    "state": str(status.get("state", "unknown")),
                    "message": str(status.get("message", "")),
                }
            except Exception as exc:
                # Suppress the reconnecting modal during the grace period after
                # an intentional Klipper restart so the user isn't alarmed by
                # the brief Moonraker downtime.
                if time.monotonic() < _klipper_restart_grace_until:
                    return {
                        "is_printing": False,
                        "state": "unknown",
                        "message": str(exc),
                    }
                state._set_printer_connecting_modal(True, str(exc))
                return {
                    "is_printing": False,
                    "state": "unknown",
                    "message": str(exc),
                }

        async def _refresh_print_state_async() -> None:
            """Run Moonraker status query off the UI path to keep GUI responsive."""
            nonlocal _print_state_refresh_inflight
            _print_state_refresh_inflight = True
            try:
                status = await _query_print_state_status()
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
            return

    def refresh_printer_card_statuses() -> None:
        """Refresh status snapshots for all configured printer cards."""
        nonlocal _printer_card_status_refresh_inflight
        if _should_skip_printer_card_status_refresh(_printer_card_status_refresh_inflight):
            return

        async def _update_single_printer_card_status(raw_profile: object) -> None:
            """Update status cache for one printer profile payload."""
            if state.printer_cards_container is None or state.printer_cards_container.is_deleted:
                return
            if not isinstance(raw_profile, dict):
                return
            profile_id = _to_int(raw_profile.get("id"), default=0)
            if profile_id <= 0:
                return
            try:
                status = await asyncio.to_thread(service.query_printer_status_for_profile, profile_id, 2.0)
            except Exception as exc:
                status = {
                    "connected": False,
                    "state": "unknown",
                    "message": str(exc),
                }
            state.printer_card_status[profile_id] = status
            if bool(status.get("connected", False)):
                state.printer_card_last_seen[profile_id] = datetime.now()

        async def _refresh_printer_card_statuses_async() -> None:
            nonlocal _printer_card_status_refresh_inflight
            _printer_card_status_refresh_inflight = True
            try:
                profiles = service.list_printer_profiles()
                for raw in profiles:
                    await _update_single_printer_card_status(raw)
            except Exception as exc:
                # Keep best-effort polling isolated from the main UI flow.
                if not state.start_page_status_label.is_deleted:
                    state.start_page_status_label.set_text(t("Printer status refresh failed: {error}", error=exc))
            finally:
                _printer_card_status_refresh_inflight = False
                if state.printer_cards_container is not None and not state.printer_cards_container.is_deleted:
                    render_printer_cards()

        try:
            asyncio.create_task(_refresh_printer_card_statuses_async())
        except RuntimeError:
            return

    def _should_skip_printer_card_status_refresh(refresh_inflight: bool) -> bool:
        """Return True when printer-card status polling should not run."""
        if state.printer_cards_container is None or state.printer_cards_container.is_deleted:
            return True
        if refresh_inflight:
            return True
        return False

    def test_standard_profile_connection() -> None:
        """Run active SSH profile connectivity test and report status."""
        if not standard_mode_enabled:
            return
        if _active_printer_is_virtual():
            message = t("Virtual printer profile uses local-only mode and does not require a connection test.")
            state.status_label.set_text(message)
            ui.notify(message, type="info")
            return
        try:
            result = service.test_active_ssh_connection()
        except Exception as exc:
            message = t("SSH profile test failed: {error}", error=exc)
            state.status_label.set_text(message)
            ui.notify(message, type="negative")
            return

        if bool(result.get("ok", False)):
            elapsed_ms = _to_int(result.get("elapsed_ms", 0), default=0)
            profile_name = str(result.get("profile_name", "")).strip() or t("unnamed")
            message = t("SSH profile '{profile}' connected in {elapsed}ms.", profile=profile_name, elapsed=elapsed_ms)
            state.status_label.set_text(message)
            ui.notify(message, type="positive")
            refresh_standard_profile_state()
            return

        error_text = str(result.get("error", "")).strip() or t("unknown error")
        message = t("SSH profile test failed: {error}", error=error_text)
        state.status_label.set_text(message)
        ui.notify(message, type="warning")

    def _set_auth_mode_fields() -> None:
        """Update secret input label for currently selected auth mode."""
        auth_mode = str(state.ssh_profile_auth_mode_select.value or "key").strip().lower()
        if auth_mode == "password":
            state.ssh_profile_secret_input.props("type=password")
            state.ssh_profile_secret_mode_label.set_text(t("Secret input expects SSH password."))
            state.ssh_profile_secret_input.update()
            return
        state.ssh_profile_secret_input.props("type=text")
        state.ssh_profile_secret_mode_label.set_text(t("Secret input expects SSH key path."))
        state.ssh_profile_secret_input.update()

    def _refresh_ssh_profile_action_buttons() -> None:
        """Enable profile actions only when a saved profile is selected."""
        selected_option = str(state.ssh_profile_select.value or "").strip()
        has_selection = selected_option in state.ssh_profile_option_ids
        state.delete_ssh_profile_button.set_enabled(has_selection)
        state.activate_ssh_profile_button.set_enabled(has_selection)

    def _set_selected_profile_secret_state(profile: dict[str, object] | None) -> None:
        """Show whether selected profile currently has stored credentials."""
        if not isinstance(profile, dict) or not profile:
            state.ssh_profile_secret_state_label.set_text(t("Secret status: set credentials when saving profile."))
            state.ssh_profile_secret_state_label.classes(replace="text-xs text-grey-5")
            return

        auth_mode = str(profile.get("auth_mode", "key")).strip().lower() or "key"
        has_secret = bool(profile.get("has_secret", False))
        backend = str(profile.get("secret_backend", "")).strip()
        backend_suffix = f" ({backend})" if backend else ""
        if has_secret:
            state.ssh_profile_secret_state_label.set_text(t("Secret status: configured") + backend_suffix)
            state.ssh_profile_secret_state_label.classes(replace="text-xs text-positive")
            return

        secret_type_label = t("password") if auth_mode == "password" else t("key path")
        state.ssh_profile_secret_state_label.set_text(
            t("Secret status: missing {secret_type}; enter and save.", secret_type=secret_type_label)
        )
        state.ssh_profile_secret_state_label.classes(replace="text-xs text-warning")

    def reset_ssh_profile_form_for_new() -> None:
        """Reset dialog fields for creating a fresh SSH profile."""
        state.ssh_profile_select.set_value("")
        state.ssh_profile_name_input.set_value("")
        state.ssh_profile_host_input.set_value("")
        state.ssh_profile_port_input.set_value(22)
        state.ssh_profile_username_input.set_value("")
        state.ssh_profile_remote_dir_input.set_value("~/printer_data/config")
        state.ssh_profile_moonraker_url_input.set_value("http://127.0.0.1:7125")
        state.ssh_profile_auth_mode_select.set_value("password")
        state.ssh_profile_secret_input.set_value("")
        state.ssh_profile_active_toggle.set_value(True)
        state.ssh_profile_error_label.set_text("")
        state.ssh_profile_status_label.set_text(t("Enter details to create a new SSH profile."))
        _set_selected_profile_secret_state(None)
        _set_auth_mode_fields()
        _refresh_ssh_profile_action_buttons()

    def _sync_moonraker_url_host(host: str) -> None:
        """Keep the Moonraker URL host aligned with the current SSH host field."""
        raw_url = str(state.ssh_profile_moonraker_url_input.value or "").strip()
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
        state.ssh_profile_moonraker_url_input.set_value(urlunparse(rewritten_url))

    def _load_selected_ssh_profile() -> None:
        """Populate profile form from the selected saved profile."""
        selected_option = str(state.ssh_profile_select.value or "").strip()
        selected_id = state.ssh_profile_option_ids.get(selected_option, 0)
        profile = state.ssh_profiles_by_id.get(int(selected_id), {}) if selected_id > 0 else {}
        if not profile:
            _refresh_ssh_profile_action_buttons()
            return

        state.ssh_profile_name_input.set_value(str(profile.get("profile_name", "")))
        state.ssh_profile_host_input.set_value(str(profile.get("host", "")))
        state.ssh_profile_port_input.set_value(_to_int(profile.get("port"), default=22))
        state.ssh_profile_username_input.set_value(str(profile.get("username", "")))
        state.ssh_profile_remote_dir_input.set_value(str(profile.get("remote_config_dir", "")))
        state.ssh_profile_moonraker_url_input.set_value(str(profile.get("moonraker_url", "")))
        auth_mode = str(profile.get("auth_mode", "key")).strip().lower() or "key"
        if auth_mode not in {"key", "password"}:
            auth_mode = "key"
        state.ssh_profile_auth_mode_select.set_value(auth_mode)
        _set_auth_mode_fields()
        state.ssh_profile_active_toggle.set_value(bool(profile.get("is_active", False)))
        _set_selected_profile_secret_state(profile)
        _refresh_ssh_profile_action_buttons()

    def refresh_ssh_profiles_dialog() -> None:
        """Refresh saved SSH profiles and sync dialog controls."""
        state.ssh_profile_error_label.set_text("")
        state.ssh_profile_status_label.set_text("")
        try:
            profiles = service.list_ssh_profiles()
        except Exception as exc:
            state.ssh_profile_error_label.set_text(t("Failed to load SSH profiles: {error}", error=exc))
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
        state.ssh_profile_select.set_options(options, value=selected_value)
        if selected_value:
            _load_selected_ssh_profile()
        else:
            reset_ssh_profile_form_for_new()
        _refresh_ssh_profile_action_buttons()

    def _selected_ssh_profile_context() -> tuple[int, dict[str, object], bool]:
        """Resolve selected SSH profile id, payload, and existing-secret status."""
        selected_option = str(state.ssh_profile_select.value or "").strip()
        selected_id = state.ssh_profile_option_ids.get(selected_option, 0)
        selected_profile = state.ssh_profiles_by_id.get(int(selected_id), {}) if selected_id > 0 else {}
        selected_has_secret = bool(selected_profile.get("has_secret", False)) if isinstance(selected_profile, dict) else False
        return int(selected_id), selected_profile if isinstance(selected_profile, dict) else {}, selected_has_secret

    def _read_ssh_profile_form_values() -> dict[str, object]:
        """Read normalized SSH profile form values from dialog controls."""
        return {
            "profile_name": str(state.ssh_profile_name_input.value or "").strip(),
            "host": str(state.ssh_profile_host_input.value or "").strip(),
            "username": str(state.ssh_profile_username_input.value or "").strip(),
            "remote_config_dir": str(state.ssh_profile_remote_dir_input.value or "").strip(),
            "moonraker_url": str(state.ssh_profile_moonraker_url_input.value or "").strip(),
            "auth_mode": str(state.ssh_profile_auth_mode_select.value or "key").strip().lower() or "key",
            "port": _to_int(state.ssh_profile_port_input.value, default=22),
            "secret_value": str(state.ssh_profile_secret_input.value or "").strip(),
            "is_active": bool(state.ssh_profile_active_toggle.value),
        }

    def _validate_ssh_profile_form_values(values: dict[str, object], *, selected_has_secret: bool) -> str | None:
        """Return localized validation error for SSH profile form or None when valid."""
        profile_name = str(values.get("profile_name", ""))
        host = str(values.get("host", ""))
        username = str(values.get("username", ""))
        remote_config_dir = str(values.get("remote_config_dir", ""))
        moonraker_url = str(values.get("moonraker_url", ""))
        auth_mode = str(values.get("auth_mode", "key")).strip().lower() or "key"
        port = _to_int(values.get("port"), default=22)
        secret_value = str(values.get("secret_value", ""))

        if not profile_name:
            return t("Profile name is required.")
        if not host:
            return t("Host is required.")
        if not username:
            return t("Username is required.")
        if not remote_config_dir:
            return t("Remote config directory is required.")
        if port < 1 or port > 65535:
            return t("Port must be between 1 and 65535.")
        if auth_mode not in {"key", "password"}:
            return t("Authentication mode must be key or password.")

        parsed_url = urlparse(moonraker_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            return t("Moonraker URL must start with http:// or https:// and include a host.")

        if not secret_value and not selected_has_secret:
            secret_type_label = t("password") if auth_mode == "password" else t("key path")
            return t("Enter SSH {secret_type} before saving.", secret_type=secret_type_label)
        return None

    def _refresh_after_ssh_profile_mutation(*, hide_editor: bool = False) -> None:
        """Refresh dependent UI state after SSH profile create/update/delete/activate."""
        refresh_printer_profile_selector()
        refresh_standard_profile_state()
        refresh_print_state()
        if hide_editor:
            _hide_printer_editor()

    def save_ssh_profile_from_dialog() -> None:
        """Persist one SSH profile and optional credentials from form values."""
        if not standard_mode_enabled:
            return
        state.ssh_profile_error_label.set_text("")
        state.ssh_profile_status_label.set_text("")

        _, _, selected_has_secret = _selected_ssh_profile_context()
        values = _read_ssh_profile_form_values()
        validation_error = _validate_ssh_profile_form_values(values, selected_has_secret=selected_has_secret)
        if validation_error:
            state.ssh_profile_error_label.set_text(validation_error)
            return

        try:
            result = service.save_ssh_profile(
                profile_name=str(values.get("profile_name", "")),
                host=str(values.get("host", "")),
                port=_to_int(values.get("port"), default=22),
                username=str(values.get("username", "")),
                remote_config_dir=str(values.get("remote_config_dir", "")),
                moonraker_url=str(values.get("moonraker_url", "")),
                auth_mode=str(values.get("auth_mode", "key")),
                is_active=bool(values.get("is_active", False)),
                secret_value=str(values.get("secret_value", "")) or None,
            )
        except Exception as exc:
            state.ssh_profile_error_label.set_text(t("Failed to save SSH profile: {error}", error=exc))
            return

        profile_id = _to_int(result.get("profile_id"), default=0)
        backend = str(result.get("secret_backend", "")).strip()
        backend_text = backend if backend else t("unchanged")
        state.ssh_profile_status_label.set_text(
            t("Profile saved. Secret backend: {backend}", backend=backend_text)
        )
        state.ssh_profile_secret_input.set_value("")
        refresh_ssh_profiles_dialog()
        if profile_id > 0:
            for option_label, option_profile_id in state.ssh_profile_option_ids.items():
                if option_profile_id == profile_id:
                    state.ssh_profile_select.set_value(option_label)
                    break
        _load_selected_ssh_profile()
        _refresh_after_ssh_profile_mutation(hide_editor=True)

    def activate_selected_ssh_profile() -> None:
        """Activate the profile selected in the management dialog."""
        selected_id, selected_profile, _ = _selected_ssh_profile_context()
        if selected_id <= 0:
            state.ssh_profile_error_label.set_text(t("Select a profile to activate."))
            return

        state.ssh_profile_error_label.set_text("")
        state.ssh_profile_status_label.set_text("")
        try:
            result = service.activate_ssh_profile(selected_id)
        except Exception as exc:
            state.ssh_profile_error_label.set_text(t("Failed to activate SSH profile: {error}", error=exc))
            return
        if not bool(result.get("ok", False)):
            state.ssh_profile_error_label.set_text(t("Failed to activate SSH profile."))
            return

        profile_name = str(selected_profile.get("profile_name", "")).strip()
        service.ensure_printer_profile_for_ssh_profile(
            ssh_profile_id=int(selected_id),
            profile_name=profile_name or t("Printer"),
            activate=True,
        )

        state.ssh_profile_status_label.set_text(t("Active SSH profile updated."))
        refresh_ssh_profiles_dialog()
        _refresh_after_ssh_profile_mutation()

    def delete_selected_ssh_profile() -> None:
        """Delete selected profile from profile storage."""
        selected_id, _, _ = _selected_ssh_profile_context()
        if selected_id <= 0:
            state.ssh_profile_error_label.set_text(t("Select a profile to delete."))
            return

        state.ssh_profile_error_label.set_text("")
        state.ssh_profile_status_label.set_text("")
        try:
            result = service.delete_ssh_profile(selected_id)
        except Exception as exc:
            state.ssh_profile_error_label.set_text(t("Failed to delete SSH profile: {error}", error=exc))
            return
        if not bool(result.get("ok", False)):
            state.ssh_profile_error_label.set_text(t("Failed to delete SSH profile."))
            return

        state.ssh_profile_status_label.set_text(t("SSH profile deleted."))
        refresh_ssh_profiles_dialog()
        _refresh_after_ssh_profile_mutation()

    def open_standard_profile_dialog() -> None:
        """Focus printer management controls on the start page."""
        refresh_printer_profile_selector()
        if standard_mode_enabled:
            refresh_ssh_profiles_dialog()
            _set_auth_mode_fields()
        refresh_printer_card_statuses()
        _set_view("start")

    def _show_remote_cfg_list_error(exc: Exception) -> None:
        """Render remote cfg list dialog in failed-load state and notify user."""
        remote_cfg_list_subtitle.set_text(t("Failed to load remote cfg files."))
        remote_cfg_list_error.set_text(t("{error}", error=exc))
        ui.notify(t("Failed to load remote cfg files: {error}", error=exc), type="negative")
        remote_cfg_list_dialog.open()

    def _render_remote_cfg_list(result: dict[str, object]) -> None:
        """Render remote cfg file list result in dialog and notify summary."""
        profile_name = str(result.get("profile_name", "")).strip() or t("unnamed")
        count = _to_int(result.get("count", 0), default=0)
        remote_cfg_list_title.set_text(t("Remote cfg files"))
        remote_cfg_list_subtitle.set_text(
            t("Profile: {profile} | Files: {count}", profile=profile_name, count=count)
        )
        files = result.get("files", [])
        file_lines: list[str] = []
        if isinstance(files, list):
            file_lines = [str(path) for path in files if str(path).strip()]
        remote_cfg_list_text.set_value("\n".join(file_lines))
        ui.notify(t("Loaded {count} remote cfg file(s).", count=count), type="info")
        remote_cfg_list_dialog.open()

    def open_remote_cfg_list_dialog() -> None:
        """Load and display remote cfg file list for active SSH profile."""
        if not standard_mode_enabled:
            return

        remote_cfg_list_error.set_text("")
        remote_cfg_list_text.set_value("")
        try:
            result = service.list_active_remote_cfg_files()
        except Exception as exc:
            _show_remote_cfg_list_error(exc)
            return

        _render_remote_cfg_list(result)

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

    def _setup_periodic_updates() -> None:
        """Register periodic background timers for UI refresh."""

        def _flush_search() -> None:
            if state._search_dirty:
                state._search_dirty = False
                render_macro_list()

        register_periodic_updates(
            flush_search=_flush_search,
            check_online_updates_on_startup=_check_online_updates_on_startup,
            refresh_create_pr_progress_ui=_refresh_create_pr_progress_ui,
            refresh_online_update_progress_ui=_refresh_online_update_progress_ui,
            check_config_changes=check_config_changes,
            refresh_standard_profile_state=refresh_standard_profile_state,
            refresh_printer_card_statuses=refresh_printer_card_statuses,
        )

    def _initialize_ui_runtime() -> None:
        """Initialize UI state and run first refresh cycle after wiring handlers."""
        _run_startup_status_refresh()
        _refresh_save_config_button()
        state.deferred_startup_scan = True
        refresh_data()
        _set_view("start")

    def _run_startup_status_refresh() -> None:
        """Refresh startup profile and printer status state used by the start page."""
        refresh_printer_profile_selector()
        refresh_ssh_profiles_dialog()
        if standard_mode_enabled and _printer_profile_missing():
            state.status_label.set_text(t("No printer configured. Complete the connection setup below."))

        if standard_mode_enabled:
            refresh_standard_profile_state()
        refresh_print_state()
        refresh_printer_card_statuses()

    def _build_footer() -> None:
        """Render bottom status/footer bar."""
        with ui.footer().classes("items-center justify-end px-4 py-1 bg-grey-9 text-grey-3"):
            ui.label(f"KlipperVault v{app_version}").classes("text-xs")

    def _setup_macro_action_menu_items() -> None:
        """Attach macro and developer menu actions."""
        nonlocal macro_migration_menu_item, macro_migration_menu_item_wrapper
        nonlocal developer_menu_import_cfg_item, developer_menu_export_update_item, developer_menu_create_pr_item
        with macro_actions_menu:
            ui.menu_item(t("Backup"), on_click=open_backup_dialog)
            with ui.element("div") as macro_migration_menu_item_wrapper_ref:
                macro_migration_menu_item_wrapper = macro_migration_menu_item_wrapper_ref
                macro_migration_menu_item = ui.menu_item(t("Migrate printer.cfg macros"), on_click=_perform_macro_migration)
            macro_migration_menu_item_wrapper.set_visibility(False)
            ui.menu_item(t("Export macros"), on_click=open_export_dialog)
            ui.menu_item(t("Import macros"), on_click=open_import_dialog)
            ui.menu_item(t("Loading order overview"), on_click=open_load_order_overview_dialog)
            ui.menu_item(t("Check for updates"), on_click=open_online_update_dialog)

        if state.developer_menu is not None:
            with state.developer_menu:
                ui.menu_item(t("Create Virtual Printer"), on_click=open_create_virtual_printer_dialog)
                developer_menu_import_cfg_item = ui.menu_item(t("Import macro.cfg"), on_click=open_import_cfg_dialog)
                developer_menu_import_cfg_item.set_visibility(False)
                developer_menu_export_update_item = ui.menu_item(t("Export Update Zip"), on_click=export_online_update_repo_zip)
                developer_menu_export_update_item.set_visibility(False)
                developer_menu_create_pr_item = ui.menu_item(t("Create Pull Request"), on_click=open_create_pr_dialog)
                developer_menu_create_pr_item.set_visibility(False)

    def _setup_filter_and_duplicate_handlers() -> None:
        """Wire macro list filter, sort, and duplicate wizard controls."""
        update_duplicates_button_label()
        update_new_button_label()
        update_active_filter_button_label()
        state.sort_radio.on_value_change(on_sort_change)
        duplicate_keep_select.on_value_change(_on_duplicate_keep_change)
        duplicate_compare_with_select.on_value_change(_on_duplicate_compare_with_change)
        duplicate_compare_button.on_click(open_duplicate_pair_compare)
        duplicate_prev_button.on_click(duplicate_wizard_previous)
        duplicate_next_button.on_click(duplicate_wizard_next)
        duplicate_apply_button.on_click(apply_duplicate_resolution)
        state.duplicates_button.on_click(toggle_duplicates_filter)
        state.new_button.on_click(toggle_new_filter)
        state.active_filter_button.on_click(cycle_active_filter)
        state.macro_search.on_value_change(on_search_change)
        duplicate_warning_button.on_click(open_duplicate_wizard)

    def _setup_profile_and_ssh_handlers() -> None:
        """Wire printer profile and SSH editor controls."""
        state.refresh_printers_button.on_click(refresh_printer_profile_selector)
        state.add_printer_button.on_click(_open_add_printer_editor)
        state.test_active_printer_button.on_click(test_standard_profile_connection)
        state.standard_cfg_list_button.on_click(open_remote_cfg_list_dialog)
        state.standard_cfg_list_button.set_visibility(standard_mode_enabled and (not _active_printer_is_virtual()))
        state.ssh_profile_select.on_value_change(_load_selected_ssh_profile)
        state.ssh_profile_auth_mode_select.on_value_change(_set_auth_mode_fields)
        state.hide_printer_editor_button.on_click(_hide_printer_editor)
        state.refresh_ssh_profiles_button.on_click(refresh_ssh_profiles_dialog)
        state.new_ssh_profile_button.on_click(_open_add_printer_editor)
        state.delete_ssh_profile_button.on_click(delete_selected_ssh_profile)
        state.activate_ssh_profile_button.on_click(activate_selected_ssh_profile)
        state.save_ssh_profile_button.on_click(save_ssh_profile_from_dialog)
        back_to_printers_button.on_click(open_standard_profile_dialog)

    def _setup_operation_handlers() -> None:
        """Wire macro operations, toolbar actions, and pagination controls."""
        def _delete_printer_profile():
            profile_id = int(state.printer_delete_profile_id)
            if profile_id <= 0:
                return
            try:
                result = service.delete_printer_profile(profile_id)
                if result.get("ok"):
                    if state.printer_delete_dialog and not state.printer_delete_dialog.is_deleted:
                        state.printer_delete_dialog.close()
                    refresh_printer_profile_selector()
                else:
                    error_msg = str(result.get("error", "Unknown error"))
                    if state.printer_delete_error_label and not state.printer_delete_error_label.is_deleted:
                        state.printer_delete_error_label.set_text(error_msg)
            except Exception as exc:
                if state.printer_delete_error_label and not state.printer_delete_error_label.is_deleted:
                    state.printer_delete_error_label.set_text(str(exc))
        
        reload_dynamic_macros_button.on_click(reload_dynamic_macros)
        restart_klipper_button.on_click(restart_klipper)
        create_backup_button.on_click(perform_backup)
        confirm_export_button.on_click(perform_export)
        confirm_import_button.on_click(perform_import)
        confirm_import_cfg_button.on_click(perform_import_cfg)
        confirm_create_pr_button.on_click(perform_create_pr)
        confirm_create_virtual_printer_button.on_click(perform_create_virtual_printer)
        confirm_online_update_button.on_click(perform_online_update_import)
        state.purge_deleted_button.on_click(purge_deleted_macros)
        confirm_restore_button.on_click(perform_restore)
        confirm_delete_button.on_click(perform_delete_backup)
        confirm_printer_delete_button.on_click(_delete_printer_profile)
        confirm_macro_delete_button.on_click(confirm_macro_delete)
        confirm_macro_migration_button.on_click(_perform_macro_migration)
        decline_macro_migration_button.on_click(_decline_macro_migration_prompt)
        index_button.on_click(run_index)
        save_config_button.on_click(save_config_to_printer)
        settings_toolbar_button.on_click(open_app_settings_dialog)
        state.prev_page_button.on_click(_go_prev_page)
        state.next_page_button.on_click(_go_next_page)

    def _setup_event_handlers() -> None:
        """Wire UI callbacks, menu actions, and control event handlers."""
        _setup_filter_and_duplicate_handlers()
        _setup_profile_and_ssh_handlers()
        _setup_macro_action_menu_items()
        _setup_operation_handlers()

    _setup_event_handlers()

    _initialize_ui_runtime()
    _setup_periodic_updates()
    _build_footer()
