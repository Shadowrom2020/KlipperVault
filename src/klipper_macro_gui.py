#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""NiceGUI frontend for Klipper macro indexing."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path

from nicegui import ui

from klipper_macro_compare import MacroCompareView
from klipper_macro_gui_logic import (
    duplicate_count_from_stats,
    duplicate_names_for_macros,
    filter_macros,
    find_active_override,
    macro_key,
    selected_or_first_macro,
    sort_macros,
)
from klipper_macro_gui_service import MacroGuiService
from klipper_macro_viewer import MacroViewer, format_ts as _format_ts
from klipper_macro_watcher import ConfigWatcher
from klipper_vault_config import load_or_create as _load_vault_config
from klipper_vault_config import save as _save_vault_config
from klipper_vault_i18n import set_language, t


DEFAULT_CONFIG_DIR = str((Path.home() / "printer_data" / "config").resolve())
DEFAULT_DB_PATH = str((Path.home() / "printer_data" / "db" / "klipper_macros.db").resolve())

_STATUS_BADGE_CLASSES: dict[str, str] = {
    "deleted": "text-[10px] uppercase tracking-wide text-white bg-grey-6 rounded px-1.5 py-0.5",
    "renamed": "text-[10px] uppercase tracking-wide text-white bg-blue-8 rounded px-1.5 py-0.5",
    "active": "text-[10px] uppercase tracking-wide text-white bg-green-8 rounded px-1.5 py-0.5",
    "inactive": "text-[10px] uppercase tracking-wide text-black bg-yellow-6 rounded px-1.5 py-0.5",
}


def _to_int(value: object, default: int = 0) -> int:
    """Convert dynamic dictionary payload values to int with fallback."""
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


def _as_dict_list(value: object) -> list[dict[str, object]]:
    """Normalize dynamic values into a list of dict payloads."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _to_optional_int(value: object) -> int | None:
    """Convert dynamic payload value to int or None when unavailable."""
    if value is None:
        return None
    return _to_int(value)


def build_ui(app_version: str = "unknown") -> None:
    """Build the full NiceGUI interface and wire all callbacks."""
    config_dir = Path(DEFAULT_CONFIG_DIR).expanduser().resolve()
    db_path = Path(DEFAULT_DB_PATH).expanduser().resolve()
    # Load (or create) klippervault.cfg once at startup. All subsequent indexing
    # runs read settings from this object without re-reading the file.
    vault_cfg = _load_vault_config(config_dir)
    set_language(os.environ.get("KLIPPERVAULT_LANG", vault_cfg.ui_language))
    ui.page_title(t("Klipper Vault"))
    service = MacroGuiService(
        db_path=db_path,
        config_dir=config_dir,
        version_history_size=vault_cfg.version_history_size,
        moonraker_base_url=os.environ.get("MOONRAKER_BASE_URL", "http://127.0.0.1:7125"),
    )

    # ── Top toolbar ──────────────────────────────────────────────────────────
    with ui.header().classes("items-center gap-4 px-6 py-2 bg-grey-9"):
        ui.label(t("Klipper Vault")).classes("text-xl font-bold text-white")
        ui.space()
        duplicate_warning_button = ui.button(t("Duplicates found"), icon="warning").props("flat no-caps")
        duplicate_warning_button.classes("text-yellow-5")
        duplicate_warning_button.set_visibility(False)
        backup_button = ui.button(t("Backup"), icon="save").props("flat color=white")
        index_button = ui.button(t("Scan macros"), icon="search").props("flat color=white")

    selected_key: str | None = None
    force_latest_for_key: str | None = None
    cached_macros: list[dict[str, object]] = []
    duplicate_wizard_groups: list[dict[str, object]] = []
    duplicate_keep_choices: dict[str, str] = {}
    duplicate_compare_with_choices: dict[str, str] = {}
    duplicate_wizard_index: int = 0
    search_query: str = ""
    show_duplicates_only: bool = False
    active_filter: str = "all"
    sort_order: str = "load_order"
    is_indexing: bool = False
    deleted_macro_count: int = 0
    printer_is_printing: bool = False
    print_lock_popup_open: bool = False
    watcher = ConfigWatcher(config_dir)
    duplicate_compare_view = MacroCompareView()

    def flat_dialog_button(label_key: str, on_click) -> None:
        """Render a standard flat no-caps dialog action button."""
        ui.button(t(label_key), on_click=on_click).props("flat no-caps")

    with ui.dialog().props("persistent") as print_lock_dialog, ui.card().classes("w-[34rem] max-w-[96vw]"):
        ui.label(t("Printer is currently printing")).classes("text-lg font-semibold text-warning")
        print_lock_label = ui.label(
            t("Macro editing and auto file watching are disabled until the print job is finished.")
        ).classes("text-sm text-grey-5")
        with ui.row().classes("w-full justify-end mt-2"):
            ui.button(t("OK"), on_click=print_lock_dialog.close).props("flat no-caps")

    with ui.dialog().props("persistent") as printer_profile_dialog, ui.card().classes("w-[34rem] max-w-[96vw]"):
        ui.label(t("Printer profile setup")).classes("text-lg font-semibold")
        ui.label(t("Please provide your printer vendor and model for first-time setup.")).classes("text-sm text-grey-5")
        printer_vendor_input = ui.input(label=t("Printer vendor")).props("outlined autofocus").classes("w-full mt-2")
        printer_model_input = ui.input(label=t("Printer model")).props("outlined").classes("w-full mt-2")
        printer_profile_error = ui.label("").classes("text-sm text-negative mt-1")
        with ui.row().classes("w-full justify-end gap-2 mt-3"):
            save_printer_profile_button = ui.button(t("Save")).props("color=primary no-caps")

    def _printer_profile_missing() -> bool:
        """Return True when first-start printer profile values are not yet set."""
        return not str(vault_cfg.printer_vendor or "").strip() or not str(vault_cfg.printer_model or "").strip()

    def _format_printer_profile_label() -> str:
        """Format printer identity for status sidebar display."""
        vendor = str(vault_cfg.printer_vendor or "").strip()
        model = str(vault_cfg.printer_model or "").strip()
        if vendor and model:
            return t("Printer profile: {vendor} {model}", vendor=vendor, model=model)
        return t("Printer profile: not set")

    def _save_printer_profile() -> None:
        """Validate and persist printer profile values into klippervault.cfg."""
        vendor = str(printer_vendor_input.value or "").strip()
        model = str(printer_model_input.value or "").strip()
        if not vendor or not model:
            printer_profile_error.set_text(t("Vendor and model are required."))
            return

        vault_cfg.printer_vendor = vendor
        vault_cfg.printer_model = model
        _save_vault_config(config_dir, vault_cfg)
        printer_profile_label.set_text(_format_printer_profile_label())
        printer_profile_error.set_text("")
        printer_profile_dialog.close()

    save_printer_profile_button.on_click(_save_printer_profile)

    with ui.grid().classes("w-full grid-cols-4 gap-4 p-4 h-[calc(100vh-110px)]"):
        with ui.card().classes("col-span-1 h-full flex flex-col overflow-hidden"):
            ui.label(t("Indexed macros")).classes("text-lg font-semibold mb-2 shrink-0")
            search_input = ui.input(placeholder=t("Search macros…")).props("clearable dense outlined").classes("w-full mb-1 shrink-0")
            with ui.row().classes("items-center gap-2 mb-1 shrink-0"):
                duplicates_button = ui.button(t("Show duplicates")).props("flat dense no-caps")
                active_filter_button = ui.button(t("Filter: {state}", state="all")).props("flat dense no-caps")
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
            macro_list = ui.list().props("separator").classes("w-full overflow-y-auto flex-1 min-h-0")

        viewer = MacroViewer()

        with ui.card().classes("col-span-1 h-full overflow-auto"):
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
            printer_profile_label = ui.label(_format_printer_profile_label()).classes("text-sm text-grey-5")

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

    restore_target_id: int | None = None
    restore_target_name = ""
    with ui.dialog() as restore_dialog, ui.card().classes("w-[30rem] max-w-[96vw]"):
        ui.label(t("Restore backup")).classes("text-lg font-semibold")
        restore_confirm_label = ui.label("").classes("text-sm text-grey-5")
        restore_error_label = ui.label("").classes("text-sm text-negative mt-1")
        with ui.row().classes("w-full justify-end gap-2 mt-3"):
            flat_dialog_button("Cancel", restore_dialog.close)
            confirm_restore_button = ui.button(t("Restore")).props("color=warning no-caps")

    delete_target_id: int | None = None
    delete_target_name = ""
    with ui.dialog() as delete_dialog, ui.card().classes("w-[30rem] max-w-[96vw]"):
        ui.label(t("Delete backup")).classes("text-lg font-semibold")
        delete_confirm_label = ui.label("").classes("text-sm text-grey-5")
        delete_error_label = ui.label("").classes("text-sm text-negative mt-1")
        with ui.row().classes("w-full justify-end gap-2 mt-3"):
            flat_dialog_button("Cancel", delete_dialog.close)
            confirm_delete_button = ui.button(t("Delete")).props("color=negative no-caps")

    macro_delete_target: dict[str, object] | None = None
    with ui.dialog() as macro_delete_dialog, ui.card().classes("w-[30rem] max-w-[96vw]"):
        ui.label(t("Delete macro from cfg file")).classes("text-lg font-semibold")
        macro_delete_confirm_label = ui.label("").classes("text-sm text-grey-5")
        macro_delete_error_label = ui.label("").classes("text-sm text-negative mt-1")
        with ui.row().classes("w-full justify-end gap-2 mt-3"):
            flat_dialog_button("Cancel", macro_delete_dialog.close)
            confirm_macro_delete_button = ui.button(t("Delete")).props("color=negative no-caps")

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

    def open_macro_by_identity(file_path: str, macro_name: str) -> None:
        """Select a macro by identity, clearing filters if needed to reveal it."""
        nonlocal selected_key

        # Ensure the active target is visible after link navigation.
        # If filters hide it, clear filters and search first.
        nonlocal show_duplicates_only
        nonlocal active_filter
        nonlocal search_query
        show_duplicates_only = False
        active_filter = "all"
        search_query = ""
        search_input.value = ""
        search_input.update()
        update_duplicates_button_label()
        update_active_filter_button_label()

        for macro in cached_macros:
            if str(macro.get("file_path", "")) == file_path and str(macro.get("macro_name", "")) == macro_name:
                selected_key = macro_key(macro)
                break
        render_macro_list()

    viewer.set_open_macro_handler(open_macro_by_identity)

    def blocked_by_print_state(
        *,
        status_message: str,
        local_error_label: ui.label | None = None,
    ) -> bool:
        """Set consistent blocked messages when printer is currently printing."""
        if not printer_is_printing:
            return False
        if local_error_label is not None:
            local_error_label.set_text(t("Blocked while printer is printing."))
        status_label.set_text(t(status_message))
        return True

    def remove_deleted_macro_from_db(file_path: str, macro_name: str) -> None:
        """Permanently remove selected deleted macro from SQLite history."""
        if blocked_by_print_state(status_message="Blocked: printer is currently printing. Editing is disabled."):
            return
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

    viewer.set_remove_deleted_handler(remove_deleted_macro_from_db)

    def restore_macro_version_from_viewer(version_row: dict) -> None:
        """Restore selected macro version into cfg file, then rescan."""
        nonlocal force_latest_for_key
        if blocked_by_print_state(status_message="Blocked: printer is currently printing. Editing is disabled."):
            return
        file_path = str(version_row.get("file_path", ""))
        macro_name = str(version_row.get("macro_name", ""))
        version = _to_int(version_row.get("version", 0) or 0)
        is_deleted = bool(version_row.get("is_deleted", False))

        if not file_path or not macro_name or version <= 0:
            status_label.set_text(t("Cannot restore macro version: missing or invalid version data."))
            return

        try:
            result = service.restore_version(file_path, macro_name, version)
        except Exception as exc:
            status_label.set_text(t("Failed to restore macro version: {error}", error=exc))
            return

        action = t("Restored deleted macro") if is_deleted else t("Reverted macro")
        status_label.set_text(t(
            "{action} '{macro_name}' from {file_path} to v{version}. Re-indexing...",
            action=action,
            macro_name=result["macro_name"],
            file_path=result["file_path"],
            version=result["version"],
        ))
        force_latest_for_key = f"{result['file_path']}::{result['macro_name']}"
        perform_index("macro restore")

    viewer.set_restore_version_handler(restore_macro_version_from_viewer)

    def save_macro_edit_from_viewer(version_row: dict, section_text: str) -> None:
        """Save edited macro text back into its source cfg file and re-index."""
        nonlocal force_latest_for_key

        if printer_is_printing:
            raise ValueError("Blocked: printer is currently printing. Editing is disabled.")

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

        result = service.save_macro_editor_text(file_path, macro_name, section_text)
        status_label.set_text(t(
            "Saved macro '{macro_name}' in {file_path} ({operation}). Re-indexing...",
            macro_name=result["macro_name"],
            file_path=result["file_path"],
            operation=result["operation"],
        ))
        force_latest_for_key = f"{result['file_path']}::{result['macro_name']}"
        perform_index("macro edit")

    viewer.set_save_macro_edit_handler(save_macro_edit_from_viewer)

    def _perform_delete_macro_source(version_row: dict) -> None:
        """Delete selected macro section from cfg file and re-index."""
        nonlocal force_latest_for_key

        if printer_is_printing:
            raise ValueError(t("Blocked: printer is currently printing. Editing is disabled."))

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

        status_label.set_text(t(
            "Deleted macro '{macro_name}' from {file_path} ({removed} section(s)). Re-indexing...",
            macro_name=result["macro_name"],
            file_path=result["file_path"],
            removed=removed,
        ))
        force_latest_for_key = f"{result['file_path']}::{result['macro_name']}"
        perform_index("macro delete")

    def delete_macro_source_from_viewer(version_row: dict) -> None:
        """Open confirmation dialog before deleting selected macro from cfg."""
        nonlocal macro_delete_target

        file_path = str(version_row.get("file_path", ""))
        macro_name = str(version_row.get("macro_name", ""))
        macro_delete_target = version_row
        macro_delete_error_label.set_text("")
        macro_delete_confirm_label.set_text(t(
            "Delete macro '{macro_name}' from {file_path}? This removes it from the cfg file. It can still be restored from the vault until it is permanently removed.",
            macro_name=macro_name or "-",
            file_path=file_path or "-",
        ))
        macro_delete_dialog.open()

    def confirm_macro_delete() -> None:
        """Execute confirmed macro deletion from the viewer dialog."""
        nonlocal macro_delete_target

        if macro_delete_target is None:
            macro_delete_error_label.set_text(t("Selected entry data is not available."))
            return

        try:
            _perform_delete_macro_source(macro_delete_target)
        except Exception as exc:
            macro_delete_error_label.set_text(str(exc))
            return

        macro_delete_dialog.close()
        macro_delete_target = None

    viewer.set_delete_macro_from_cfg_handler(delete_macro_source_from_viewer)

    def update_duplicates_button_label() -> None:
        """Sync duplicates filter button text with current filter state."""
        duplicates_button.set_text(t("Show all macros") if show_duplicates_only else t("Show duplicates"))

    def update_active_filter_button_label() -> None:
        """Sync active/inactive cycle button text with current filter state."""
        active_filter_button.set_text(t("Filter: {state}", state=active_filter))

    def status_badge_key(macro: dict[str, object]) -> str:
        """Resolve macro row status key for consistent badge rendering."""
        if macro.get("is_deleted", False):
            return "deleted"
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
        nonlocal sort_order
        sort_order = e.value
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
        macro_name = str(duplicate_wizard_groups[duplicate_wizard_index].get("macro_name", ""))
        compare_options = {
            str(entry.get("file_path", "")): str(entry.get("file_path", ""))
            for entry in entries
            if str(entry.get("file_path", "")) != keep_file
        }
        duplicate_compare_with_select.options = compare_options

        selected_compare = duplicate_compare_with_choices.get(macro_name)
        if not selected_compare or selected_compare not in compare_options:
            selected_compare = next(iter(compare_options), "")
            duplicate_compare_with_choices[macro_name] = selected_compare
        duplicate_compare_with_select.value = selected_compare
        duplicate_compare_with_select.update()

        duplicate_compare_button.set_enabled(bool(compare_options))

    def _render_duplicate_wizard_step() -> None:
        """Render one duplicate macro group in the wizard."""
        if not duplicate_wizard_groups:
            return

        group = duplicate_wizard_groups[duplicate_wizard_index]
        macro_name = str(group.get("macro_name", ""))
        entries = _as_dict_list(group.get("entries", []))

        duplicate_wizard_title.set_text(t("Resolve duplicates: {macro_name}", macro_name=macro_name))
        duplicate_wizard_subtitle.set_text(
            t("Step {index} of {total}", index=duplicate_wizard_index + 1, total=len(duplicate_wizard_groups))
        )

        duplicate_entry_list.clear()
        with duplicate_entry_list:
            for entry in entries:
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    ui.label(str(entry.get("file_path", "-"))).classes("flex-1 text-sm")
                    ui.label(f"v{entry.get('version', '-')}").classes("text-[11px] text-grey-5")
                    if entry.get("is_active", False):
                        render_status_badge("active")

        options = {
            str(entry.get("file_path", "")): str(entry.get("file_path", ""))
            for entry in entries
        }
        duplicate_keep_select.options = options

        selected_file = duplicate_keep_choices.get(macro_name)
        if not selected_file or selected_file not in options:
            selected_file = _default_keep_file(entries)
            duplicate_keep_choices[macro_name] = selected_file

        duplicate_keep_select.value = selected_file
        duplicate_keep_select.update()
        _update_duplicate_compare_choice(entries, selected_file)
        duplicate_wizard_error.set_text("")

        duplicate_prev_button.set_enabled(duplicate_wizard_index > 0)
        duplicate_next_button.set_visibility(duplicate_wizard_index < len(duplicate_wizard_groups) - 1)
        duplicate_apply_button.set_visibility(duplicate_wizard_index == len(duplicate_wizard_groups) - 1)

    def _on_duplicate_keep_change(e) -> None:
        """Persist selected keep target for current duplicate group."""
        if not duplicate_wizard_groups:
            return
        macro_name = str(duplicate_wizard_groups[duplicate_wizard_index].get("macro_name", ""))
        keep_file = str(e.value or "")
        duplicate_keep_choices[macro_name] = keep_file
        entries = _as_dict_list(duplicate_wizard_groups[duplicate_wizard_index].get("entries", []))
        _update_duplicate_compare_choice(entries, keep_file)

    def _on_duplicate_compare_with_change(e) -> None:
        """Persist selected compare target for current duplicate group."""
        if not duplicate_wizard_groups:
            return
        macro_name = str(duplicate_wizard_groups[duplicate_wizard_index].get("macro_name", ""))
        duplicate_compare_with_choices[macro_name] = str(e.value or "")

    def open_duplicate_pair_compare() -> None:
        """Open side-by-side compare view for currently selected duplicate pair."""
        if not duplicate_wizard_groups:
            duplicate_wizard_error.set_text(t("No duplicates loaded."))
            return

        group = duplicate_wizard_groups[duplicate_wizard_index]
        macro_name = str(group.get("macro_name", ""))
        keep_file = str(duplicate_keep_choices.get(macro_name, ""))
        compare_file = str(duplicate_compare_with_choices.get(macro_name, ""))
        if not keep_file or not compare_file:
            duplicate_wizard_error.set_text(t("Select two definitions to compare."))
            return
        if keep_file == compare_file:
            duplicate_wizard_error.set_text(t("Choose a different definition for comparison."))
            return

        keep_macro = _load_latest_macro_for_file(macro_name, keep_file)
        compare_macro = _load_latest_macro_for_file(macro_name, compare_file)
        if keep_macro is None or compare_macro is None:
            duplicate_wizard_error.set_text(t("Could not load one or both macro definitions."))
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
        duplicate_compare_view.set_macro({"macro_name": macro_name}, compare_versions)
        duplicate_compare_view.open()

    def open_duplicate_wizard() -> None:
        """Open duplicate-resolution wizard from toolbar warning button."""
        nonlocal duplicate_wizard_groups
        nonlocal duplicate_keep_choices
        nonlocal duplicate_compare_with_choices
        nonlocal duplicate_wizard_index

        if printer_is_printing:
            status_label.set_text(t("Blocked: printer is currently printing. Duplicate resolution is disabled."))
            return

        duplicate_wizard_groups = service.list_duplicates()
        if not duplicate_wizard_groups:
            status_label.set_text(t("No duplicates found."))
            return

        backup_name = datetime.now().strftime("Resolve_Duplicates-%Y%m%d-%H%M%S")
        try:
            backup_result = service.create_backup(backup_name)
        except Exception as exc:
            status_label.set_text(t("Failed to create pre-resolve backup: {error}", error=exc))
            return

        duplicate_keep_choices = {}
        duplicate_compare_with_choices = {}
        duplicate_wizard_index = 0
        _render_duplicate_wizard_step()
        duplicate_wizard_dialog.open()
        status_label.set_text(t(
            "Created pre-resolve backup '{backup_name}' with {macro_count} macro(s).",
            backup_name=backup_result["backup_name"],
            macro_count=backup_result["macro_count"],
        ))
        render_backup_list()

    def duplicate_wizard_previous() -> None:
        """Navigate to previous duplicate group."""
        nonlocal duplicate_wizard_index
        if duplicate_wizard_index <= 0:
            return
        duplicate_wizard_index -= 1
        _render_duplicate_wizard_step()

    def duplicate_wizard_next() -> None:
        """Navigate to next duplicate group."""
        nonlocal duplicate_wizard_index
        if duplicate_wizard_index >= len(duplicate_wizard_groups) - 1:
            return
        duplicate_wizard_index += 1
        _render_duplicate_wizard_step()

    def apply_duplicate_resolution() -> None:
        """Apply keep choices by deleting duplicate sections from cfg files."""
        if printer_is_printing:
            duplicate_wizard_error.set_text(t("Blocked while printer is printing."))
            return
        if not duplicate_wizard_groups:
            duplicate_wizard_error.set_text(t("No duplicates loaded."))
            return

        missing = [
            str(group.get("macro_name", ""))
            for group in duplicate_wizard_groups
            if not duplicate_keep_choices.get(str(group.get("macro_name", "")))
        ]
        if missing:
            duplicate_wizard_error.set_text(t("Select a keep target for every macro before applying."))
            return

        keep_map = {
            str(group.get("macro_name", "")): str(duplicate_keep_choices[str(group.get("macro_name", ""))])
            for group in duplicate_wizard_groups
        }

        try:
            result = service.resolve_duplicates(keep_choices=keep_map, duplicate_groups=duplicate_wizard_groups)
        except Exception as exc:
            duplicate_wizard_error.set_text(t("Failed to resolve duplicates: {error}", error=exc))
            return

        duplicate_wizard_dialog.close()
        touched_files_raw = result.get("touched_files", [])
        touched_files_count = len(touched_files_raw) if isinstance(touched_files_raw, list) else 0
        status_label.set_text(t(
            "Removed {removed_sections} duplicate section(s) in {file_count} file(s). Re-indexing...",
            removed_sections=result["removed_sections"],
            file_count=touched_files_count,
        ))
        perform_index("duplicate wizard")

    def render_macro_list() -> None:
        """Render the left macro list with filters, badges, and selection state."""
        nonlocal selected_key
        nonlocal force_latest_for_key
        macro_list.clear()
        viewer.set_available_macros(cached_macros)

        duplicate_names = duplicate_names_for_macros(cached_macros)
        visible_macros = filter_macros(
            macros=cached_macros,
            search_query=search_query,
            show_duplicates_only=show_duplicates_only,
            active_filter=active_filter,
            duplicate_names=duplicate_names,
        )
        visible_macros = sort_macros(visible_macros, sort_order)
        query = search_query.strip().lower()
        filter_active = bool(query) or show_duplicates_only or active_filter != "all"
        macro_count_label.set_text(
            t("Items: {visible} / {total}", visible=len(visible_macros), total=len(cached_macros))
            if filter_active
            else t("Items: {visible}", visible=len(visible_macros))
        )

        if not visible_macros:
            with macro_list:
                ui.item(t("No macros indexed yet.") if not cached_macros else t("No matches."))
            viewer.set_macro(None, [])
            return

        selected_macro = selected_or_first_macro(visible_macros, selected_key)
        if selected_macro is None:
            viewer.set_macro(None, [])
            return
        selected_key = macro_key(selected_macro)

        versions = service.load_versions(
            str(selected_macro["file_path"]),
            str(selected_macro["macro_name"]),
        )

        def choose_macro(macro: dict[str, object]) -> None:
            nonlocal selected_key
            selected_key = macro_key(macro)
            render_macro_list()

        with macro_list:
            for macro in visible_macros:
                button_classes = "flex-1 justify-start normal-case text-left items-start"
                file_label_classes = "text-[11px]"
                if macro_key(macro) == selected_key:
                    button_classes += " bg-blue-9 text-white"
                    file_label_classes += " text-blue-1"
                else:
                    file_label_classes += " text-grey-5"
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    file_name = Path(str(macro["file_path"])).name
                    is_deleted = bool(macro.get("is_deleted", False))
                    vc = _to_int(macro.get("version_count", 1), default=1)
                    with ui.button(on_click=lambda m=macro: choose_macro(m)).props(
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

        active_macro = find_active_override(selected_macro, cached_macros)

        selected_macro_key = macro_key(selected_macro)
        prefer_latest = force_latest_for_key == selected_macro_key
        if prefer_latest:
            force_latest_for_key = None

        viewer.set_macro(
            selected_macro,
            versions,
            active_macro=active_macro,
            prefer_latest=prefer_latest,
        )

    def render_backup_list() -> None:
        """Render right-panel backup entries and attach action handlers."""
        nonlocal restore_target_id
        nonlocal restore_target_name
        nonlocal delete_target_id
        nonlocal delete_target_name
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
            backup_view_dialog.open()

        def open_restore_dialog(backup: dict[str, object]) -> None:
            """Prepare and open restore confirmation dialog for one backup."""
            nonlocal restore_target_id
            nonlocal restore_target_name
            restore_target_id = _to_int(backup.get("backup_id", 0))
            restore_target_name = str(backup.get("backup_name", "-")).strip() or "-"
            restore_error_label.set_text("")
            restore_confirm_label.set_text(
                t(
                    "Restore backup '{backup_name}'? This replaces the current indexed macro state.",
                    backup_name=restore_target_name,
                )
            )
            restore_dialog.open()

        def open_delete_dialog(backup: dict[str, object]) -> None:
            """Prepare and open delete confirmation dialog for one backup."""
            nonlocal delete_target_id
            nonlocal delete_target_name
            delete_target_id = _to_int(backup.get("backup_id", 0))
            delete_target_name = str(backup.get("backup_name", "-")).strip() or "-"
            delete_error_label.set_text("")
            delete_confirm_label.set_text(
                t("Delete backup '{backup_name}'? This cannot be undone.", backup_name=delete_target_name)
            )
            delete_dialog.open()

        with backup_list:
            for backup in backups:
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    ui.label(str(backup.get("backup_name", "-")).strip() or "-").classes(
                        "flex-1 text-sm"
                    )
                    ui.label(_format_ts(_to_int(backup.get("created_at", 0)))).classes(
                        "text-[11px] text-grey-5"
                    )
                    ui.button(icon="search", on_click=lambda b=backup: open_backup_contents(b)).props(
                        "flat dense round"
                    ).classes("text-blue-5")
                    ui.button(icon="restore", on_click=lambda b=backup: open_restore_dialog(b)).props(
                        "flat dense round"
                    ).classes("text-orange-6")
                    ui.button(icon="delete", on_click=lambda b=backup: open_delete_dialog(b)).props(
                        "flat dense round"
                    ).classes("text-red-6")

    def perform_restore() -> None:
        """Restore backup to DB and cfg files, then rescan to reflect on-disk state."""
        if blocked_by_print_state(
            status_message="Blocked: printer is currently printing. Restore is disabled.",
            local_error_label=restore_error_label,
        ):
            return
        if restore_target_id is None:
            restore_error_label.set_text(t("No backup selected."))
            return

        try:
            result = service.restore_backup(restore_target_id)
        except Exception as exc:
            restore_error_label.set_text(t("Restore failed: {error}", error=exc))
            status_label.set_text(t("Restore failed: {error}", error=exc))
            return

        restore_dialog.close()
        restored_label = _format_ts(_to_int(result.get("restored_at", 0)))
        rewritten = _to_int(result.get("restored_cfg_files", 0))
        if rewritten > 0:
            status_label.set_text(
                t(
                    "Restored backup '{backup_name}' at {restored_at} with {macro_count} macro(s); rewrote {cfg_file_count} cfg file(s). Re-indexing...",
                    backup_name=result["backup_name"],
                    restored_at=restored_label,
                    macro_count=result["macro_count"],
                    cfg_file_count=rewritten,
                )
            )
        else:
            status_label.set_text(
                t(
                    "Restored backup '{backup_name}' at {restored_at} with {macro_count} macro(s). "
                    "No cfg snapshot was stored in this backup; only DB state was restored. Re-indexing...",
                    backup_name=result["backup_name"],
                    restored_at=restored_label,
                    macro_count=result["macro_count"],
                )
            )
        perform_index("backup restore")

    def perform_delete_backup() -> None:
        """Delete selected backup and refresh the backup list."""
        if blocked_by_print_state(
            status_message="Blocked: printer is currently printing. Delete is disabled.",
            local_error_label=delete_error_label,
        ):
            return
        if delete_target_id is None:
            delete_error_label.set_text(t("No backup selected."))
            return

        try:
            result = service.delete_backup(delete_target_id)
        except Exception as exc:
            delete_error_label.set_text(t("Delete failed: {error}", error=exc))
            status_label.set_text(t("Delete failed: {error}", error=exc))
            return

        delete_dialog.close()
        status_label.set_text(t("Deleted backup '{backup_name}'.", backup_name=result["backup_name"]))
        render_backup_list()

    def refresh_data() -> None:
        """Reload all list/stats data from SQLite and rerender UI sections."""
        nonlocal cached_macros
        nonlocal deleted_macro_count
        stats, cached_macros = service.load_dashboard()
        deleted_macros = _to_int(stats.get("deleted_macros", 0))
        deleted_macro_count = deleted_macros
        duplicate_macros = duplicate_count_from_stats(stats)
        duplicate_warning_button.set_visibility(duplicate_macros > 0)
        total_macros_label.set_text(t("Total macros: {count}", count=stats["total_macros"]))
        duplicate_macros_label.set_text(t("Duplicate macros: {count}", count=duplicate_macros))
        deleted_macros_label.set_text(t("Deleted macros: {count}", count=deleted_macros))
        purge_deleted_button.set_visibility(deleted_macros > 0)
        distinct_files_label.set_text(t("Config files: {count}", count=stats["distinct_cfg_files"]))
        last_update_label.set_text(t("Last update: {value}", value=_format_ts(_to_optional_int(stats.get("latest_update_ts")))))

        render_macro_list()
        render_backup_list()

    def perform_index(trigger: str) -> None:
        """Run cfg indexing and refresh UI when complete."""
        nonlocal is_indexing
        if is_indexing:
            return
        is_indexing = True
        try:
            status_label.set_text(t("Scanning macros ({trigger})...", trigger=trigger))
            result = service.index()
            status_label.set_text(
                t(
                    "Stored {inserted} new version(s), {unchanged} unchanged - {scanned} .cfg files scanned",
                    inserted=result["macros_inserted"],
                    unchanged=result["macros_unchanged"],
                    scanned=result["cfg_files_scanned"],
                )
            )
            refresh_data()
            watcher.reset()
        except FileNotFoundError as exc:
            status_label.set_text(t("Error: {error}", error=exc))
        finally:
            is_indexing = False

    def open_backup_dialog() -> None:
        """Open backup creation dialog with generated default name."""
        backup_error_label.set_text("")
        backup_name_input.value = datetime.now().strftime("backup-%Y%m%d-%H%M%S")
        backup_name_input.update()
        backup_dialog.open()

    def perform_backup() -> None:
        """Create named backup snapshot and update status/list output."""
        if printer_is_printing:
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

        backup_dialog.close()
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

    def purge_deleted_macros() -> None:
        """Remove all deleted macro histories from SQLite in one action."""
        if printer_is_printing:
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
        if printer_is_printing:
            status_label.set_text(t("Blocked: printer is currently printing. Manual scan is disabled."))
            return
        perform_index("manual")

    def set_print_lock(locked: bool, moonraker_state: str, moonraker_message: str) -> None:
        """Toggle UI mutation lock while printer is actively printing."""
        nonlocal printer_is_printing
        nonlocal print_lock_popup_open

        printer_is_printing = locked
        editing_enabled = not locked

        index_button.set_enabled(editing_enabled)
        backup_button.set_enabled(editing_enabled)
        duplicate_warning_button.set_enabled(editing_enabled)
        purge_deleted_button.set_enabled(editing_enabled and deleted_macro_count > 0)
        create_backup_button.set_enabled(editing_enabled)
        confirm_restore_button.set_enabled(editing_enabled)
        confirm_delete_button.set_enabled(editing_enabled)
        duplicate_compare_button.set_enabled(editing_enabled)
        duplicate_prev_button.set_enabled(editing_enabled and duplicate_wizard_index > 0)
        duplicate_next_button.set_enabled(editing_enabled and duplicate_wizard_index < len(duplicate_wizard_groups) - 1)
        duplicate_apply_button.set_enabled(editing_enabled)
        viewer.set_editing_enabled(editing_enabled)

        if locked:
            if moonraker_message:
                print_lock_label.set_text(
                    t(
                        "Macro editing and auto file watching are disabled until the print job is finished. "
                        "Moonraker message: {moonraker_message}",
                        moonraker_message=moonraker_message,
                    )
                )
            else:
                print_lock_label.set_text(
                    t("Macro editing and auto file watching are disabled until the print job is finished.")
                )
            if not print_lock_popup_open:
                print_lock_dialog.open()
                print_lock_popup_open = True
            status_label.set_text(
                t(
                    "Printing in progress ({state}). File watcher paused and editing disabled.",
                    state=moonraker_state,
                )
            )
        else:
            if print_lock_popup_open:
                print_lock_dialog.close()
                print_lock_popup_open = False
            if moonraker_state == "unknown":
                status_label.set_text(t("Ready (Moonraker status unknown)."))
            else:
                status_label.set_text(t("Ready (printer state: {state}).", state=moonraker_state))

    def refresh_print_state() -> None:
        """Poll Moonraker printer state and apply UI lock policy."""
        status = service.query_printer_status(timeout=1.5)
        set_print_lock(
            locked=bool(status.get("is_printing", False)),
            moonraker_state=str(status.get("state", "unknown")),
            moonraker_message=str(status.get("message", "")),
        )

    def check_config_changes() -> None:
        """Timer callback: auto-rescan when cfg files change."""
        refresh_print_state()
        if printer_is_printing:
            return
        if is_indexing:
            return
        if watcher.poll_changed():
            perform_index("watcher")

    def toggle_duplicates_filter() -> None:
        """Toggle duplicate-only filter and rerender list."""
        nonlocal show_duplicates_only
        show_duplicates_only = not show_duplicates_only
        update_duplicates_button_label()
        render_macro_list()

    def cycle_active_filter() -> None:
        """Cycle active filter through all -> active -> inactive."""
        nonlocal active_filter
        if active_filter == "all":
            active_filter = "active"
        elif active_filter == "active":
            active_filter = "inactive"
        else:
            active_filter = "all"
        update_active_filter_button_label()
        render_macro_list()

    def on_search_change(e) -> None:
        """Search input change handler."""
        nonlocal search_query
        search_query = e.value or ""
        render_macro_list()

    update_duplicates_button_label()
    update_active_filter_button_label()
    sort_radio.on_value_change(on_sort_change)
    duplicate_keep_select.on_value_change(_on_duplicate_keep_change)
    duplicate_compare_with_select.on_value_change(_on_duplicate_compare_with_change)
    duplicate_compare_button.on_click(open_duplicate_pair_compare)
    duplicate_prev_button.on_click(duplicate_wizard_previous)
    duplicate_next_button.on_click(duplicate_wizard_next)
    duplicate_apply_button.on_click(apply_duplicate_resolution)
    duplicates_button.on_click(toggle_duplicates_filter)
    active_filter_button.on_click(cycle_active_filter)
    search_input.on_value_change(on_search_change)
    duplicate_warning_button.on_click(open_duplicate_wizard)
    backup_button.on_click(open_backup_dialog)
    create_backup_button.on_click(perform_backup)
    purge_deleted_button.on_click(purge_deleted_macros)
    confirm_restore_button.on_click(perform_restore)
    confirm_delete_button.on_click(perform_delete_backup)
    confirm_macro_delete_button.on_click(confirm_macro_delete)

    index_button.on_click(run_index)

    if _printer_profile_missing():
        printer_profile_dialog.open()

    refresh_print_state()
    if not printer_is_printing:
        perform_index("startup")
    else:
        refresh_data()
    watcher.reset()
    ui.timer(2.0, check_config_changes)

    with ui.footer().classes("items-center justify-end px-4 py-1 bg-grey-9 text-grey-3"):
        ui.label(f"KlipperVault v{app_version}").classes("text-xs")
