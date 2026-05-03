#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Centralized UI state container for KlipperVault NiceGUI interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from nicegui import ui

from klipper_macro_compare import MacroCompareView


@dataclass
class UIState:
    """Consolidated runtime state and UI elements for build_ui()."""

    # ─── Service/Config References ───────────────────────────────────────────
    service: object  # MacroGuiService
    config_dir: object  # Path
    app_version: str = "unknown"

    # ─── Macro List State ────────────────────────────────────────────────────
    selected_key: str | None = None
    force_latest_for_key: str | None = None
    force_active_for_key: str | None = None
    cached_macros: list[dict[str, object]] = field(default_factory=list)
    search_query: str = ""
    show_duplicates_only: bool = False
    show_new_only: bool = False
    active_filter: str = "all"
    sort_order: str = "load_order"
    is_indexing: bool = False
    deleted_macro_count: int = 0
    list_page_size: int = 200
    list_page_index: int = 0
    total_macro_rows: int = 0
    _search_dirty: bool = False
    _cached_versions_key: str | None = None
    _cached_versions: list[dict[str, object]] = field(default_factory=list)
    cached_duplicate_names: set[str] = field(default_factory=set)

    # ─── Duplicate Resolution State ──────────────────────────────────────────
    duplicate_wizard_groups: list[dict[str, object]] = field(default_factory=list)
    duplicate_keep_choices: dict[str, str] = field(default_factory=dict)
    duplicate_compare_with_choices: dict[str, str] = field(default_factory=dict)
    duplicate_wizard_index: int = 0
    duplicate_compare_view: MacroCompareView = field(default_factory=MacroCompareView)

    # ─── Printer State ───────────────────────────────────────────────────────
    printer_is_printing: bool = False
    printer_is_busy: bool = True
    printer_state: str = "unknown"
    printer_status_message: str = ""
    printer_seen_connected: bool = False
    restart_required: bool = False
    dynamic_reload_required: bool = False
    print_lock_popup_open: bool = False
    has_unsynced_local_changes: bool = False

    # ─── Off-Printer State ───────────────────────────────────────────────────
    standard_profile_ready: bool = True
    standard_profile_status_text: str = ""
    ssh_profile_option_ids: dict[str, int] = field(default_factory=dict)
    ssh_profiles_by_id: dict[int, dict[str, object]] = field(default_factory=dict)
    printer_profile_option_ids: dict[str, int] = field(default_factory=dict)

    printer_connecting_modal_open: bool = False

    # ─── View Navigation State ───────────────────────────────────────────────
    current_view: str = "start"  # start | macro
    selected_printer_profile_id: int = 0
    printer_card_status: dict[int, dict[str, object]] = field(default_factory=dict)
    printer_card_last_seen: dict[int, datetime] = field(default_factory=dict)

    # ─── Activity Tracking ──────────────────────────────────────────────────
    last_activity_monotonic: float = 0.0

    # ─── Online Updates & Imports ────────────────────────────────────────────
    uploaded_import_bytes: bytes | None = None
    uploaded_import_name: str = ""
    uploaded_cfg_import_bytes: bytes | None = None
    uploaded_cfg_import_name: str = ""
    pending_online_updates: list[dict[str, object]] = field(default_factory=list)
    online_update_check_in_progress: bool = False
    online_update_progress_current: int = 0
    online_update_progress_total: int = 1
    create_pr_in_progress: bool = False
    create_pr_progress_current: int = 0
    create_pr_progress_total: int = 1
    startup_online_update_check_in_progress: bool = False
    deferred_startup_scan: bool = False

    # ─── UI Element References ──────────────────────────────────────────────
    # Toolbar elements
    toolbar_header: ui.header | None = None
    macro_actions_button: ui.button | None = None
    macro_actions_menu: ui.menu | None = None
    developer_menu: ui.menu | None = None
    reload_dynamic_macros_button: ui.button | None = None
    restart_klipper_button: ui.button | None = None
    duplicate_warning_button: ui.button | None = None
    backup_button: ui.button | None = None
    index_button: ui.button | None = None
    save_config_button: ui.button | None = None

    # Status/Connection labels
    status_label: ui.label | None = None
    standard_profile_label: ui.label | None = None

    # Main layout elements
    macro_list_container: ui.column | None = None
    macro_list: ui.select | None = None
    macro_search: ui.input | None = None
    macro_status_badges: ui.row | None = None
    detail_panel: ui.card | None = None
    viewer: object | None = None  # MacroViewer instance
    start_page_container: ui.column | None = None
    macro_page_container: ui.column | None = None
    printer_cards_container: ui.row | None = None

    # Macro page controls
    duplicates_button: ui.button | None = None
    new_button: ui.button | None = None
    active_filter_button: ui.button | None = None
    sort_radio: object | None = None  # ui.radio
    macro_count_label: ui.label | None = None
    prev_page_button: ui.button | None = None
    next_page_button: ui.button | None = None
    total_macros_label: ui.label | None = None
    duplicate_macros_label: ui.label | None = None
    deleted_macros_label: ui.label | None = None
    distinct_files_label: ui.label | None = None
    last_update_label: ui.label | None = None
    purge_deleted_button: ui.button | None = None
    standard_cfg_list_button: ui.button | None = None
    backup_list: object | None = None  # ui.list

    # Start page controls
    start_page_status_label: ui.label | None = None
    refresh_printers_button: ui.button | None = None
    add_printer_button: ui.button | None = None
    test_active_printer_button: ui.button | None = None
    printer_editor_card: object | None = None  # ui.card
    printer_editor_title: ui.label | None = None
    hide_printer_editor_button: ui.button | None = None
    refresh_ssh_profiles_button: ui.button | None = None
    new_ssh_profile_button: ui.button | None = None
    delete_ssh_profile_button: ui.button | None = None
    activate_ssh_profile_button: ui.button | None = None
    save_ssh_profile_button: ui.button | None = None

    # SSH profile inputs
    ssh_profile_select: object | None = None  # ui.select
    ssh_profile_name_input: ui.input | None = None
    ssh_profile_host_input: ui.input | None = None
    ssh_profile_port_input: object | None = None  # ui.number
    ssh_profile_username_input: ui.input | None = None
    ssh_profile_remote_dir_input: ui.input | None = None
    ssh_profile_moonraker_url_input: ui.input | None = None
    ssh_profile_auth_mode_select: object | None = None  # ui.select
    ssh_profile_secret_input: ui.input | None = None
    ssh_profile_secret_mode_label: ui.label | None = None
    ssh_profile_secret_state_label: ui.label | None = None
    ssh_profile_active_toggle: object | None = None  # ui.switch
    ssh_profile_error_label: ui.label | None = None
    ssh_profile_status_label: ui.label | None = None

    # Dialog references
    print_lock_dialog: ui.dialog | None = None
    print_lock_label: ui.label | None = None

    printer_connecting_dialog: ui.dialog | None = None
    printer_connecting_label: ui.label | None = None

    printer_profile_dialog: ui.dialog | None = None
    printer_vendor_input: ui.input | None = None
    printer_model_input: ui.input | None = None
    printer_profile_error: ui.label | None = None
    save_printer_profile_button: ui.button | None = None

    backup_dialog: ui.dialog | None = None
    backup_view_dialog: ui.dialog | None = None
    backup_table: ui.aggrid | None = None

    restore_dialog: ui.dialog | None = None
    restore_version_select: ui.select | None = None
    restore_confirm_label: ui.label | None = None
    restore_error_label: ui.label | None = None
    restore_target_id: int | None = None
    restore_target_name: str = ""

    delete_dialog: ui.dialog | None = None
    delete_version_select: ui.select | None = None
    delete_confirm_label: ui.label | None = None
    delete_error_label: ui.label | None = None
    delete_target_id: int | None = None
    delete_target_name: str = ""
    macro_delete_dialog: ui.dialog | None = None
    macro_delete_confirm_label: ui.label | None = None
    macro_delete_error_label: ui.label | None = None
    macro_delete_target: dict[str, object] | None = None

    export_dialog: ui.dialog | None = None
    export_macro_list: ui.column | None = None
    export_payload_preview: ui.code | None = None

    import_dialog: ui.dialog | None = None
    import_uploader: ui.upload | None = None
    import_error_label: ui.label | None = None
    import_preview_label: ui.label | None = None
    import_payload_preview: ui.code | None = None

    import_cfg_dialog: ui.dialog | None = None
    import_cfg_uploader: ui.upload | None = None
    import_cfg_error_label: ui.label | None = None

    load_order_dialog: ui.dialog | None = None
    load_order_table: ui.aggrid | None = None

    create_pr_dialog: ui.dialog | None = None
    pr_repo_url_input: ui.input | None = None
    pr_base_branch_input: ui.input | None = None
    pr_head_branch_input: ui.input | None = None
    pr_title_input: ui.input | None = None
    pr_body_input: ui.textarea | None = None
    pr_token_input: ui.input | None = None
    create_pr_error_label: ui.label | None = None
    confirm_create_pr_button: ui.button | None = None

    online_update_dialog: ui.dialog | None = None
    online_update_table: ui.aggrid | None = None
    online_update_list: ui.column | None = None
    online_update_summary_label: ui.label | None = None
    online_update_error_label: ui.label | None = None
    online_update_activate_checkboxes: dict[str, object] = field(default_factory=dict)
    confirm_online_update_button: ui.button | None = None
    duplicate_wizard_dialog: ui.dialog | None = None
    duplicate_wizard_title: ui.label | None = None
    duplicate_wizard_subtitle: ui.label | None = None
    duplicate_wizard_error: ui.label | None = None

    def get_all_state_vars(self) -> dict[str, object]:
        """Return a dict of all state variable key-value pairs (for debugging)."""
        return {
            k: v
            for k, v in vars(self).items()
            if not k.startswith("_") and not isinstance(v, ui.element)
        }
