#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""NiceGUI frontend for Klipper macro indexing."""

from __future__ import annotations

import asyncio
import gc
import ctypes
from datetime import datetime
import os
from queue import Empty, SimpleQueue
from pathlib import Path
import threading
import tempfile
import time

from nicegui import app, ui

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
from klipper_macro_gui_remote_service import RemoteMacroGuiService
from klipper_macro_viewer import MacroViewer, format_ts as _format_ts
from klipper_macro_watcher import ConfigWatcher
from klipper_vault_config import load_or_create as _load_vault_config
from klipper_vault_paths import DEFAULT_CONFIG_DIR, DEFAULT_DB_PATH
from klipper_vault_config import save as _save_vault_config
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


def _env_bool(name: str, default: bool) -> bool:
    """Return bool environment toggle with common truthy/falsey strings."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _env_float(name: str, default: float, minimum: float) -> float:
    """Return float environment value clamped to a minimum bound."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        parsed = float(raw.strip())
    except ValueError:
        return default
    return max(minimum, parsed)


def build_ui(app_version: str = "unknown") -> None:
    """Build the full NiceGUI interface and wire all callbacks."""
    config_dir = Path(DEFAULT_CONFIG_DIR).expanduser().resolve()
    db_path = Path(DEFAULT_DB_PATH).expanduser().resolve()
    # Load (or create) klippervault.cfg once at startup. All subsequent indexing
    # runs read settings from this object without re-reading the file.
    vault_cfg = _load_vault_config(config_dir)
    set_language(os.environ.get("KLIPPERVAULT_LANG", vault_cfg.ui_language))
    ui.page_title(t("Klipper Vault"))
    remote_api_url = str(
        os.environ.get("KLIPPERVAULT_REMOTE_API_URL", vault_cfg.remote_api_url)
    ).strip()
    remote_api_token = str(
        os.environ.get("KLIPPERVAULT_REMOTE_API_TOKEN", vault_cfg.remote_api_token)
    ).strip()
    remote_mode_enabled = bool(remote_api_url)
    if remote_api_url:
        service = RemoteMacroGuiService(
            base_url=remote_api_url,
            api_token=remote_api_token,
            timeout=_env_float("KLIPPERVAULT_REMOTE_API_TIMEOUT", 120.0, 1.0),
        )
    else:
        service = MacroGuiService(
            db_path=db_path,
            config_dir=config_dir,
            version_history_size=vault_cfg.version_history_size,
            moonraker_base_url=os.environ.get("MOONRAKER_BASE_URL", "http://127.0.0.1:7125"),
        )

    # ── Top toolbar ──────────────────────────────────────────────────────────
    with ui.header().classes("items-center gap-2 px-4 py-2 bg-grey-9 flex-wrap"):
        ui.label(t("Klipper Vault")).classes("text-xl font-bold text-white")
        ui.space()
        with ui.button(t("Macro actions"), icon="menu").props("flat color=white") as macro_actions_button:
            with ui.menu() as macro_actions_menu:
                pass
        developer_menu: ui.menu | None = None
        if vault_cfg.developer:
            with ui.button(t("Developer"), icon="developer_mode").props("flat color=white"):
                with ui.menu() as developer_menu:
                    pass
        reload_dynamic_macros_button = ui.button(t("Reload Dynamic Macros"), icon="autorenew").props("flat color=white")
        reload_dynamic_macros_button.classes("text-blue-4")
        reload_dynamic_macros_button.set_visibility(False)
        restart_klipper_button = ui.button(t("Restart Klipper"), icon="restart_alt").props("flat color=white")
        restart_klipper_button.classes("text-orange-4")
        restart_klipper_button.set_visibility(False)
        duplicate_warning_button = ui.button(t("Duplicates found"), icon="warning").props("flat no-caps")
        duplicate_warning_button.classes("text-yellow-5")
        duplicate_warning_button.set_visibility(False)
        backup_button = ui.button(t("Backup"), icon="save").props("flat color=white")
        index_button = ui.button(t("Scan macros"), icon="search").props("flat color=white")

    selected_key: str | None = None
    force_latest_for_key: str | None = None
    force_active_for_key: str | None = None
    cached_macros: list[dict[str, object]] = []
    duplicate_wizard_groups: list[dict[str, object]] = []
    duplicate_keep_choices: dict[str, str] = {}
    duplicate_compare_with_choices: dict[str, str] = {}
    duplicate_wizard_index: int = 0
    search_query: str = ""
    show_duplicates_only: bool = False
    show_new_only: bool = False
    active_filter: str = "all"
    sort_order: str = "load_order"
    is_indexing: bool = False
    deleted_macro_count: int = 0
    list_page_size: int = max(50, _to_int(os.environ.get("KLIPPERVAULT_LIST_PAGE_SIZE", "200"), default=200))
    list_page_index: int = 0
    total_macro_rows: int = 0
    printer_is_printing: bool = False
    printer_is_busy: bool = True
    printer_state: str = "unknown"
    remote_api_connected: bool = True
    remote_api_status_text: str = ""
    remote_event_queue: SimpleQueue[dict[str, object]] = SimpleQueue()
    remote_event_stop = threading.Event()
    remote_event_listener_thread: threading.Thread | None = None
    remote_last_event_id: int = 0
    remote_data_dirty: bool = False
    print_lock_popup_open: bool = False
    restart_required: bool = False
    dynamic_reload_required: bool = False
    memory_trim_enabled: bool = _env_bool("KLIPPERVAULT_MEMORY_TRIM", True)
    memory_trim_idle_seconds: float = _env_float("KLIPPERVAULT_MEMORY_TRIM_IDLE_SECONDS", 180.0, 15.0)
    memory_trim_cooldown_seconds: float = _env_float("KLIPPERVAULT_MEMORY_TRIM_COOLDOWN_SECONDS", 60.0, 5.0)
    memory_trim_no_clients_enabled: bool = _env_bool("KLIPPERVAULT_MEMORY_TRIM_NO_CLIENTS", True)
    memory_trim_no_clients_grace_seconds: float = _env_float(
        "KLIPPERVAULT_MEMORY_TRIM_NO_CLIENTS_GRACE_SECONDS", 120.0, 10.0
    )
    last_activity_monotonic: float = time.monotonic()
    last_trim_monotonic: float = 0.0
    connected_client_ids: set[str] = set()
    no_clients_since: float | None = None
    no_client_cache_released: bool = False
    cached_duplicate_names: set[str] = set()
    _cached_versions_key: str | None = None
    _cached_versions: list[dict[str, object]] = []
    _search_dirty: bool = False
    watcher = ConfigWatcher(config_dir)
    duplicate_compare_view = MacroCompareView()

    def _note_activity() -> None:
        """Record runtime activity to detect idle windows for memory trim."""
        nonlocal last_activity_monotonic
        last_activity_monotonic = time.monotonic()

    def _trim_process_memory(reason: str, *, force: bool = False) -> None:
        """Run conservative memory cleanup with cooldown safeguards."""
        nonlocal last_trim_monotonic
        if not memory_trim_enabled:
            return

        now = time.monotonic()
        if (not force) and (now - last_trim_monotonic) < memory_trim_cooldown_seconds:
            return

        collected = gc.collect()
        malloc_trim_result = "unavailable"
        try:
            libc = ctypes.CDLL("libc.so.6")
            malloc_trim = getattr(libc, "malloc_trim", None)
            if malloc_trim is not None:
                malloc_trim.argtypes = [ctypes.c_size_t]
                malloc_trim.restype = ctypes.c_int
                malloc_trim_result = str(int(malloc_trim(0)))
        except Exception:
            malloc_trim_result = "error"

        last_trim_monotonic = now
        print(
            f"[KlipperVault] memory-trim: reason={reason} gc_collected={collected} malloc_trim={malloc_trim_result}",
            flush=True,
        )

    def _is_any_client_connected() -> bool:
        """Return True when at least one browser client is currently connected."""
        return bool(connected_client_ids)

    def _release_ui_caches_for_no_clients() -> None:
        """Drop large UI caches when no browser is connected."""
        nonlocal cached_macros
        nonlocal cached_duplicate_names
        nonlocal _cached_versions
        nonlocal _cached_versions_key
        nonlocal selected_key

        cached_macros = []
        cached_duplicate_names = set()
        _cached_versions = []
        _cached_versions_key = None
        selected_key = None
        try:
            viewer.set_macro(None, [])
        except RuntimeError as error:
            # NiceGUI may tear down element slots before disconnect callbacks finish.
            if "parent slot of the element has been deleted" not in str(error):
                raise

    def _on_client_connect(client=None) -> None:
        """Track connected clients and restore normal active behavior."""
        nonlocal no_clients_since
        nonlocal no_client_cache_released
        client_id = getattr(client, "id", None)
        if client_id is not None:
            connected_client_ids.add(str(client_id))
        no_clients_since = None
        no_client_cache_released = False

    def _on_client_disconnect(client=None) -> None:
        """Track disconnected clients and start no-client idle window."""
        nonlocal no_clients_since
        nonlocal no_client_cache_released
        client_id = getattr(client, "id", None)
        if client_id is not None:
            connected_client_ids.discard(str(client_id))
        if not connected_client_ids and no_clients_since is None:
            no_clients_since = time.monotonic()
            if memory_trim_no_clients_enabled:
                _release_ui_caches_for_no_clients()
                no_client_cache_released = True
                _trim_process_memory("disconnect", force=True)

    app.on_connect(_on_client_connect)
    app.on_disconnect(_on_client_disconnect)

    def flat_dialog_button(label_key: str, on_click) -> None:
        """Render a standard flat no-caps dialog action button."""
        ui.button(t(label_key), on_click=on_click).props("flat no-caps")

    uploaded_import_bytes: bytes | None = None
    uploaded_import_name: str = ""
    pending_online_updates: list[dict[str, object]] = []
    online_update_check_in_progress: bool = False
    online_update_progress_current: int = 0
    online_update_progress_total: int = 1
    create_pr_in_progress: bool = False
    create_pr_progress_current: int = 0
    create_pr_progress_total: int = 1
    startup_online_update_check_in_progress: bool = False

    async def _on_import_upload(e) -> None:
        """Capture uploaded macro share file contents for import."""
        nonlocal uploaded_import_bytes
        nonlocal uploaded_import_name
        uploaded_file = getattr(e, "file", None)
        if uploaded_file is None:
            uploaded_import_bytes = None
            uploaded_import_name = ""
            import_error_label.set_text(t("Please upload a macro share file."))
            return

        uploaded_import_bytes = await uploaded_file.read()
        uploaded_import_name = str(getattr(uploaded_file, "name", "") or "")
        import_error_label.set_text("")

    def _refresh_reload_buttons() -> None:
        """Show exactly one pending reload action button when printer is idle."""
        is_allowed = (not printer_is_printing) and (not printer_is_busy)
        show_restart = restart_required and is_allowed
        # Dynamic macros can be reloaded while printing.
        show_dynamic_reload = (not restart_required) and dynamic_reload_required

        restart_klipper_button.set_enabled(show_restart)
        restart_klipper_button.set_visibility(show_restart)

        reload_dynamic_macros_button.set_enabled(show_dynamic_reload)
        reload_dynamic_macros_button.set_visibility(show_dynamic_reload)

    def _remote_actions_available() -> bool:
        """Return True when backend actions are currently available."""
        return (not remote_mode_enabled) or remote_api_connected

    def _set_remote_connection_state(connected: bool, detail: str = "") -> None:
        """Update remote connectivity indicators used for degraded read-only mode."""
        nonlocal remote_api_connected
        nonlocal remote_api_status_text

        remote_api_connected = connected
        detail_text = str(detail or "").strip()
        if connected:
            remote_api_status_text = t("Remote API connected")
            if detail_text:
                remote_api_status_text = t("Remote API connected: {detail}", detail=detail_text)
            remote_connection_label.classes(replace="text-xs text-positive")
        else:
            remote_api_status_text = t("Remote API disconnected")
            if detail_text:
                remote_api_status_text = t("Remote API disconnected: {detail}", detail=detail_text)
            remote_connection_label.classes(replace="text-xs text-negative")
        remote_connection_label.set_text(remote_api_status_text)

    def _mark_reload_required(*, is_dynamic: bool = False) -> None:
        """Mark pending runtime action after macro-affecting changes."""
        nonlocal restart_required
        nonlocal dynamic_reload_required

        if is_dynamic and not restart_required:
            dynamic_reload_required = True
        else:
            restart_required = True
            dynamic_reload_required = False
        _refresh_reload_buttons()

    def _clear_restart_required() -> None:
        """Clear pending runtime action after successful restart/reload."""
        nonlocal restart_required
        nonlocal dynamic_reload_required
        restart_required = False
        dynamic_reload_required = False
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
            for macro in cached_macros
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
        return bool(vault_cfg.printer_profile_prompt_required)

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
        vault_cfg.printer_profile_prompt_required = False
        _save_vault_config(config_dir, vault_cfg)
        printer_profile_label.set_text(_format_printer_profile_label())
        printer_profile_error.set_text("")
        printer_profile_dialog.close()

    save_printer_profile_button.on_click(_save_printer_profile)

    with ui.grid().classes("w-full grid-cols-1 md:grid-cols-3 xl:grid-cols-4 gap-4 p-4 xl:h-[calc(100vh-110px)]"):
        with ui.card().classes("col-span-1 xl:h-full flex flex-col overflow-hidden min-h-[55vh] xl:min-h-0"):
            ui.label(t("Indexed macros")).classes("text-lg font-semibold mb-2 shrink-0")
            search_input = ui.input(placeholder=t("Search macros…")).props("clearable dense outlined").classes("w-full mb-1 shrink-0")
            with ui.row().classes("items-center gap-2 mb-1 shrink-0"):
                duplicates_button = ui.button(t("Show duplicates")).props("flat dense no-caps")
                new_button = ui.button(t("Show new")).props("flat dense no-caps")
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
            with ui.row().classes("items-center gap-2 mb-1 shrink-0"):
                prev_page_button = ui.button(t("Prev"))
                prev_page_button.props("flat dense no-caps")
                next_page_button = ui.button(t("Next"))
                next_page_button.props("flat dense no-caps")
                page_label = ui.label(t("Page {current} / {total}", current=1, total=1)).classes("text-xs text-grey-5")
            macro_list = ui.list().props("separator").classes("w-full overflow-y-auto flex-1 min-h-0")

        viewer = MacroViewer()

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
            remote_connection_label = ui.label("").classes("text-xs text-grey-5")
            remote_connection_label.set_visibility(remote_mode_enabled)
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

    with ui.dialog() as export_dialog, ui.card().classes("w-[42rem] max-w-[98vw]"):
        ui.label(t("Export macros")).classes("text-lg font-semibold")
        ui.label(t("Select one or more macros to export into a share file.")).classes("text-sm text-grey-5")
        ui.label(t("Macros")).classes("text-sm mt-2")
        export_macro_list = ui.column().classes("w-full max-h-[20rem] overflow-y-auto gap-1 border rounded p-2")
        export_error_label = ui.label("").classes("text-sm text-negative mt-1")
        with ui.row().classes("w-full justify-end gap-2 mt-3"):
            flat_dialog_button("Cancel", export_dialog.close)
            confirm_export_button = ui.button(t("Export")).props("color=primary no-caps")

    export_macro_checkboxes: dict[str, object] = {}

    with ui.dialog() as import_dialog, ui.card().classes("w-[38rem] max-w-[98vw]"):
        ui.label(t("Import macros")).classes("text-lg font-semibold")
        ui.label(t("Import a shared macro file into inactive new versions.")).classes("text-sm text-grey-5")
        import_upload = ui.upload(on_upload=_on_import_upload, auto_upload=True).props("accept=.json").classes("w-full mt-2")
        import_error_label = ui.label("").classes("text-sm text-negative mt-1")
        with ui.row().classes("w-full justify-end gap-2 mt-3"):
            flat_dialog_button("Cancel", import_dialog.close)
            confirm_import_button = ui.button(t("Import")).props("color=primary no-caps")

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

    online_update_activate_checkboxes: dict[str, object] = {}
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
        nonlocal show_new_only
        nonlocal active_filter
        nonlocal search_query
        show_duplicates_only = False
        show_new_only = False
        active_filter = "all"
        search_query = ""
        search_input.value = ""
        search_input.update()
        update_duplicates_button_label()
        update_new_button_label()
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

    def remove_inactive_macro_from_db(version_row: dict) -> None:
        """Permanently remove selected inactive macro version from SQLite history."""
        nonlocal force_active_for_key
        if blocked_by_print_state(status_message="Blocked: printer is currently printing. Editing is disabled."):
            return
        file_path = str(version_row.get("file_path", ""))
        macro_name = str(version_row.get("macro_name", ""))
        version = _to_int(version_row.get("version", 0) or 0)
        if not file_path or not macro_name:
            status_label.set_text(t("Cannot remove inactive macro version: missing identity."))
            return

        try:
            result = service.remove_inactive_version(file_path, macro_name, version)
        except Exception as exc:
            status_label.set_text(t("Failed to remove inactive macro version: {error}", error=exc))
            return

        reason = str(result.get("reason", ""))
        removed = _to_int(result.get("removed", 0))
        if removed > 0:
            status_label.set_text(t(
                "Removed inactive macro version v{version} of '{macro_name}' from {file_path} ({removed} row(s)).",
                version=version,
                macro_name=macro_name,
                file_path=file_path,
                removed=removed,
            ))
            force_active_for_key = f"{file_path}::{macro_name}"
        elif reason == "not_inactive":
            status_label.set_text(t("Selected macro version is not inactive; nothing removed."))
        elif reason == "deleted":
            status_label.set_text(t("Selected macro version is deleted; use the deleted-macro removal action instead."))
        elif reason == "not_found":
            status_label.set_text(t("Macro not found in database."))
        else:
            status_label.set_text(t("No rows removed."))

        refresh_data()

    viewer.set_remove_inactive_handler(remove_inactive_macro_from_db)

    def restore_macro_version_from_viewer(version_row: dict) -> None:
        """Restore selected macro version into cfg file, then rescan."""
        nonlocal force_latest_for_key
        if printer_is_printing and not _is_dynamic_version_row(version_row):
            status_label.set_text(t("Blocked: printer is currently printing. Editing is disabled."))
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
        _mark_reload_required(is_dynamic=_is_dynamic_version_row(version_row))
        force_latest_for_key = f"{result['file_path']}::{result['macro_name']}"
        perform_index("macro restore")

    viewer.set_restore_version_handler(restore_macro_version_from_viewer)

    def save_macro_edit_from_viewer(version_row: dict, section_text: str) -> None:
        """Save edited macro text back into its source cfg file and re-index."""
        nonlocal force_latest_for_key

        if printer_is_printing and not _is_dynamic_version_row(version_row):
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
        _mark_reload_required(is_dynamic=_is_dynamic_version_row(version_row))
        force_latest_for_key = f"{result['file_path']}::{result['macro_name']}"
        perform_index("macro edit")

    viewer.set_save_macro_edit_handler(save_macro_edit_from_viewer)

    def _perform_delete_macro_source(version_row: dict) -> None:
        """Delete selected macro section from cfg file and re-index."""
        nonlocal force_latest_for_key

        if printer_is_printing and not _is_dynamic_version_row(version_row):
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
        _mark_reload_required(is_dynamic=_is_dynamic_version_row(version_row))
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

    def update_new_button_label() -> None:
        """Sync new-macros filter button text with current filter state."""
        new_button.set_text(t("Show all macros") if show_new_only else t("Show new"))

    def update_active_filter_button_label() -> None:
        """Sync active/inactive cycle button text with current filter state."""
        active_filter_button.set_text(t("Filter: {state}", state=active_filter))

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
                        render_status_badge(status_badge_key(entry))

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
        touched_files = [str(path) for path in touched_files_raw] if isinstance(touched_files_raw, list) else []
        _mark_reload_required(is_dynamic=_files_include_dynamic_macros(touched_files))
        perform_index("duplicate wizard")

    def render_macro_list() -> None:
        """Render the left macro list with filters, badges, and selection state."""
        nonlocal selected_key
        nonlocal force_latest_for_key
        nonlocal force_active_for_key
        nonlocal _cached_versions_key, _cached_versions
        macro_list.clear()
        viewer.set_available_macros(cached_macros)

        duplicate_names = cached_duplicate_names
        visible_macros = filter_macros(
            macros=cached_macros,
            search_query=search_query,
            show_duplicates_only=show_duplicates_only,
            active_filter=active_filter,
            duplicate_names=duplicate_names,
            show_new_only=show_new_only,
        )
        
        visible_macros = sort_macros(visible_macros, sort_order)
        query = search_query.strip().lower()
        filter_active = bool(query) or show_duplicates_only or show_new_only or active_filter != "all"
        macro_count_label.set_text(
            t("Items: {visible} / {total}", visible=len(visible_macros), total=total_macro_rows)
            if filter_active
            else t("Items: {visible}", visible=len(visible_macros))
        )

        total_pages = max(1, (max(total_macro_rows, 1) + list_page_size - 1) // list_page_size)
        page_label.set_text(t("Page {current} / {total}", current=list_page_index + 1, total=total_pages))
        prev_page_button.set_enabled(list_page_index > 0)
        next_page_button.set_enabled((list_page_index + 1) < total_pages)

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

        _ver_key = f"{selected_macro['file_path']}::{selected_macro['macro_name']}"
        if _ver_key != _cached_versions_key:
            _cached_versions_key = _ver_key
            _cached_versions = service.load_versions(
                str(selected_macro["file_path"]),
                str(selected_macro["macro_name"]),
            )
        versions = _cached_versions

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

        active_macro = find_active_override(selected_macro, cached_macros)

        selected_macro_key = macro_key(selected_macro)
        prefer_latest = force_latest_for_key == selected_macro_key
        prefer_active = force_active_for_key == selected_macro_key
        if prefer_latest:
            force_latest_for_key = None
        if prefer_active:
            force_active_for_key = None

        viewer.set_macro(
            selected_macro,
            versions,
            active_macro=active_macro,
            prefer_latest=prefer_latest,
            prefer_active=prefer_active,
        )
        # While printing, allow editing only for dynamic macros.
        viewer.set_editing_enabled((not printer_is_printing) or bool(selected_macro.get("is_dynamic", False)))

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
        _mark_reload_required(is_dynamic=False)
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
        nonlocal list_page_index
        nonlocal total_macro_rows
        nonlocal cached_duplicate_names, _cached_versions_key, _cached_versions
        _note_activity()
        try:
            stats, cached_macros = service.load_dashboard(limit=list_page_size, offset=list_page_index * list_page_size)
        except Exception as exc:
            if remote_mode_enabled:
                _set_remote_connection_state(False, str(exc))
            status_label.set_text(t("Data refresh failed: {error}", error=exc))
            return
        total_macro_rows = _to_int(stats.get("total_macros", len(cached_macros)), default=len(cached_macros))

        total_pages = max(1, (max(total_macro_rows, 1) + list_page_size - 1) // list_page_size)
        if list_page_index >= total_pages:
            list_page_index = max(0, total_pages - 1)
            try:
                stats, cached_macros = service.load_dashboard(limit=list_page_size, offset=list_page_index * list_page_size)
            except Exception as exc:
                if remote_mode_enabled:
                    _set_remote_connection_state(False, str(exc))
                status_label.set_text(t("Data refresh failed: {error}", error=exc))
                return
            total_macro_rows = _to_int(stats.get("total_macros", len(cached_macros)), default=len(cached_macros))

        deleted_macros = _to_int(stats.get("deleted_macros", 0))
        deleted_macro_count = deleted_macros
        cached_duplicate_names = duplicate_names_for_macros(cached_macros)
        duplicate_macros = duplicate_count_from_stats(stats)
        _cached_versions_key = None
        _cached_versions = []
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
        _note_activity()
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
            if trigger != "startup" and _to_int(result.get("macros_inserted", 0)) > 0:
                inserted = _to_int(result.get("macros_inserted", 0))
                dynamic_inserted = _to_int(result.get("dynamic_macros_inserted", 0))
                _mark_reload_required(is_dynamic=(inserted > 0 and dynamic_inserted == inserted))
            refresh_data()
            watcher.reset()
        except FileNotFoundError as exc:
            status_label.set_text(t("Error: {error}", error=exc))
        except Exception as exc:
            status_label.set_text(t("Scan failed: {error}", error=exc))
        finally:
            is_indexing = False

    def open_backup_dialog() -> None:
        """Open backup creation dialog with generated default name."""
        backup_name_input.value = datetime.now().strftime("backup-%Y%m%d-%H%M%S")
        backup_name_input.update()
        backup_dialog.open()

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
        load_order_dialog.open()

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

    def open_export_dialog() -> None:
        """Open macro export dialog with selectable latest macro identities."""
        export_macro_checkboxes.clear()
        export_macro_list.clear()
        with export_macro_list:
            for macro in cached_macros:
                identity = f"{str(macro.get('file_path', ''))}::{str(macro.get('macro_name', ''))}"
                label = f"{str(macro.get('display_name') or macro.get('macro_name', ''))} ({str(macro.get('file_path', ''))})"
                checkbox = ui.checkbox(label, value=(identity == selected_key)).props("dense")
                export_macro_checkboxes[identity] = checkbox
        export_error_label.set_text("")
        export_dialog.open()

    def perform_export() -> None:
        """Export selected macros to a share file on disk."""
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
                source_vendor=vault_cfg.printer_vendor,
                source_model=vault_cfg.printer_model,
                out_file=out_path,
            )
        except Exception as exc:
            export_error_label.set_text(t("Export failed: {error}", error=exc))
            status_label.set_text(t("Export failed: {error}", error=exc))
            return

        export_dialog.close()
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
        nonlocal uploaded_import_bytes
        nonlocal uploaded_import_name
        uploaded_import_bytes = None
        uploaded_import_name = ""
        import_upload.reset()
        import_error_label.set_text("")
        import_dialog.open()

    def perform_import() -> None:
        """Import a macro share file and refresh dashboard state."""
        if not uploaded_import_bytes:
            import_error_label.set_text(t("Please upload a macro share file."))
            return

        suffix = Path(uploaded_import_name).suffix or ".json"
        temp_import_file = Path(tempfile.gettempdir()) / (
            datetime.now().strftime("klippervault-import-%Y%m%d-%H%M%S") + suffix
        )
        temp_import_file.write_bytes(uploaded_import_bytes)

        try:
            result = service.import_macro_share_file(
                import_file=temp_import_file,
                target_vendor=vault_cfg.printer_vendor,
                target_model=vault_cfg.printer_model,
            )
        except Exception as exc:
            import_error_label.set_text(t("Import failed: {error}", error=exc))
            status_label.set_text(t("Import failed: {error}", error=exc))
            return
        finally:
            temp_import_file.unlink(missing_ok=True)

        import_dialog.close()
        imported = _to_int(result.get("imported", 0))
        source_vendor = str(result.get("source_vendor", "")).strip()
        source_model = str(result.get("source_model", "")).strip()
        if imported <= 0:
            status_label.set_text(t("No macros were imported."))
            return

        if bool(result.get("printer_matches", False)):
            status_label.set_text(t("Imported {count} macro(s) as new inactive entries.", count=imported))
        elif source_vendor and source_model:
            status_label.set_text(
                t(
                    "Imported {count} macro(s) for printer {vendor} {model}. Review before enabling.",
                    count=imported,
                    vendor=source_vendor,
                    model=source_model,
                )
            )
        else:
            status_label.set_text(
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

        generated_name = datetime.now().strftime("klippervault-online-update-repo-%Y%m%d-%H%M%S.zip")
        out_path = Path(tempfile.gettempdir()) / generated_name

        try:
            result = service.export_online_update_repository_zip(
                out_file=out_path,
                source_vendor=vault_cfg.printer_vendor,
                source_model=vault_cfg.printer_model,
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
        vendor = str(vault_cfg.printer_vendor or "").strip().lower().replace(" ", "-") or "printer"
        model = str(vault_cfg.printer_model or "").strip().lower().replace(" ", "-") or "model"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return f"klippervault/{vendor}-{model}/{stamp}"

    def _refresh_create_pr_progress_ui() -> None:
        """Sync pull-request progress widgets with current background state."""
        if not create_pr_in_progress:
            create_pr_progress_label.set_visibility(False)
            create_pr_progress_bar.set_visibility(False)
            return

        display_total = max(create_pr_progress_total, 1)
        progress_value = min(max(create_pr_progress_current / display_total, 0.0), 1.0)
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
        nonlocal create_pr_in_progress
        nonlocal create_pr_progress_current
        nonlocal create_pr_progress_total

        pr_repo_url_input.set_value(str(vault_cfg.online_update_repo_url or "").strip())
        pr_base_branch_input.set_value(str(vault_cfg.online_update_ref or "main").strip() or "main")
        pr_head_branch_input.set_value(_default_pr_head_branch())
        pr_title_input.set_value(
            t(
                "Update macros for {vendor} {model}",
                vendor=str(vault_cfg.printer_vendor or "").strip() or "printer",
                model=str(vault_cfg.printer_model or "").strip() or "model",
            )
        )
        pr_body_input.set_value(
            t(
                "Automated KlipperVault update for {vendor} {model}.",
                vendor=str(vault_cfg.printer_vendor or "").strip() or "printer",
                model=str(vault_cfg.printer_model or "").strip() or "model",
            )
        )
        pr_token_input.set_value("")
        create_pr_error_label.set_text("")
        create_pr_in_progress = False
        create_pr_progress_current = 0
        create_pr_progress_total = 1
        _refresh_create_pr_progress_ui()
        confirm_create_pr_button.set_enabled(True)
        create_pr_dialog.open()

    async def perform_create_pr() -> None:
        """Create a pull request on GitHub for current active macro artifacts."""
        nonlocal create_pr_in_progress
        nonlocal create_pr_progress_current
        nonlocal create_pr_progress_total

        if _printer_profile_missing():
            create_pr_error_label.set_text(t("Set printer vendor/model before creating a pull request."))
            return

        repo_url = str(pr_repo_url_input.value or "").strip()
        base_branch = str(pr_base_branch_input.value or "").strip()
        head_branch = str(pr_head_branch_input.value or "").strip()
        title = str(pr_title_input.value or "").strip()
        body = str(pr_body_input.value or "").strip()
        token = str(pr_token_input.value or "").strip()

        if not repo_url or not base_branch or not head_branch or not title or not token:
            create_pr_error_label.set_text(t("Repository URL, branches, title, and token are required."))
            return

        create_pr_in_progress = True
        create_pr_progress_current = 0
        create_pr_progress_total = 1
        confirm_create_pr_button.set_enabled(False)
        create_pr_error_label.set_text("")
        status_label.set_text(t("Creating GitHub pull request..."))
        _refresh_create_pr_progress_ui()
        await asyncio.sleep(0)

        def report_progress(current: int, total: int) -> None:
            nonlocal create_pr_progress_current
            nonlocal create_pr_progress_total
            create_pr_progress_current = max(int(current), 0)
            create_pr_progress_total = max(int(total), 1)

        try:
            result = await asyncio.to_thread(
                service.create_online_update_pull_request,
                source_vendor=vault_cfg.printer_vendor,
                source_model=vault_cfg.printer_model,
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
            create_pr_in_progress = False
            _refresh_create_pr_progress_ui()
            create_pr_error_label.set_text(t("Create PR failed: {error}", error=exc))
            status_label.set_text(t("Create PR failed: {error}", error=exc))
            confirm_create_pr_button.set_enabled(True)
            return

        create_pr_in_progress = False
        _refresh_create_pr_progress_ui()
        confirm_create_pr_button.set_enabled(True)
        create_pr_dialog.close()
        pr_number = _to_int(result.get("pull_request_number", 0))
        pr_url = str(result.get("pull_request_url", "")).strip()
        updated_files = _to_int(result.get("updated_files", 0))
        macro_count = _to_int(result.get("macro_count", 0))
        commit_count = _to_int(result.get("commit_count", 0))

        if bool(result.get("no_changes", False)):
            message = t("No macro changes detected for pull request. PR was not created.")
            status_label.set_text(message)
            ui.notify(message, type="warning")
            return

        if bool(result.get("existing", False)):
            status_label.set_text(
                t(
                    "Open pull request already exists (#{number}): {url}",
                    number=pr_number,
                    url=pr_url or "-",
                )
            )
            return

        status_label.set_text(
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
        if not online_update_check_in_progress:
            online_update_progress_label.set_visibility(False)
            online_update_progress_bar.set_visibility(False)
            return

        display_total = max(online_update_progress_total, 1)
        progress_value = min(max(online_update_progress_current / display_total, 0.0), 1.0)
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
        nonlocal pending_online_updates
        nonlocal online_update_check_in_progress
        nonlocal online_update_progress_current
        nonlocal online_update_progress_total

        online_update_activate_checkboxes.clear()
        online_update_list.clear()
        online_update_error_label.set_text("")
        online_update_summary_label.set_text("")
        pending_online_updates = []
        confirm_online_update_button.set_enabled(False)
        confirm_online_update_button.set_visibility(False)

        if _printer_profile_missing():
            status_label.set_text(t("Set printer vendor/model before checking updates."))
            return

        repo_url = str(vault_cfg.online_update_repo_url or "").strip()
        if not repo_url:
            status_label.set_text(t("Online updater repository URL is not configured."))
            return

        online_update_check_in_progress = True
        online_update_progress_current = 0
        online_update_progress_total = 1
        online_update_summary_label.set_text(t("Checking for updates..."))
        _refresh_online_update_progress_ui()
        online_update_dialog.open()
        await asyncio.sleep(0)

        def report_progress(current: int, total: int) -> None:
            nonlocal online_update_progress_current
            nonlocal online_update_progress_total
            online_update_progress_current = max(int(current), 0)
            online_update_progress_total = max(int(total), 0)

        try:
            result = await asyncio.to_thread(
                service.check_online_updates,
                repo_url=repo_url,
                manifest_path=vault_cfg.online_update_manifest_path,
                repo_ref=vault_cfg.online_update_ref,
                source_vendor=vault_cfg.printer_vendor,
                source_model=vault_cfg.printer_model,
                progress_callback=report_progress,
            )
        except Exception as exc:
            online_update_check_in_progress = False
            _refresh_online_update_progress_ui()
            status_label.set_text(t("Update check failed: {error}", error=exc))
            return

        online_update_check_in_progress = False
        _refresh_online_update_progress_ui()

        updates = result.get("updates", [])
        pending_online_updates = [item for item in updates if isinstance(item, dict)] if isinstance(updates, list) else []
        checked = _to_int(result.get("checked", 0))
        changed = _to_int(result.get("changed", 0))
        unchanged = _to_int(result.get("unchanged", 0))
        online_update_summary_label.set_text(
            t(
                "Checked {checked} macro(s): {changed} update(s), {unchanged} unchanged.",
                checked=checked,
                changed=changed,
                unchanged=unchanged,
            )
        )

        with online_update_list:
            if not pending_online_updates:
                ui.label(t("No online updates available.")).classes("text-sm text-grey-5")
            for item in pending_online_updates:
                identity = str(item.get("identity", ""))
                macro_name = str(item.get("macro_name", "")).strip() or t("Unnamed macro")
                local_version = _to_int(item.get("local_version", 0))
                remote_version = str(item.get("remote_version", "")).strip()
                version_label = t("local v{local} -> remote {remote}", local=local_version, remote=remote_version or "-")
                row_label = f"{macro_name} ({version_label})"
                checkbox = ui.checkbox(row_label, value=False).props("dense")
                online_update_activate_checkboxes[identity] = checkbox

            confirm_online_update_button.set_enabled(bool(pending_online_updates))
            confirm_online_update_button.set_visibility(bool(pending_online_updates))
            online_update_dialog.open()
        status_label.set_text(t("Online update check complete."))

    async def _check_online_updates_on_startup() -> None:
        """Run one background online update check on every app startup when repository is configured."""
        nonlocal startup_online_update_check_in_progress

        if startup_online_update_check_in_progress:
            return

        repo_url = str(vault_cfg.online_update_repo_url or "").strip()
        if not repo_url:
            return

        startup_online_update_check_in_progress = True
        try:
            result = await asyncio.to_thread(
                service.check_online_updates,
                repo_url=repo_url,
                manifest_path=vault_cfg.online_update_manifest_path,
                repo_ref=vault_cfg.online_update_ref,
                source_vendor=vault_cfg.printer_vendor,
                source_model=vault_cfg.printer_model,
            )
        except Exception:
            startup_online_update_check_in_progress = False
            return

        startup_online_update_check_in_progress = False

        changed = _to_int(result.get("changed", 0))
        if changed <= 0:
            return

        checked = _to_int(result.get("checked", 0))
        message = t(
            "Startup update check found {changed} update(s) out of {checked} macro(s).",
            changed=changed,
            checked=checked,
        )
        status_label.set_text(message)
        ui.notify(message, type="info")
        try:
            await asyncio.to_thread(service.send_mainsail_notification, message=message)
        except Exception as exc:
            # Keep UI flow resilient if Moonraker notification delivery fails.
            status_label.set_text(t("Mainsail notification failed: {error}", error=exc))

    def perform_online_update_import() -> None:
        """Import checked online updates and activate only selected macros."""
        if not pending_online_updates:
            online_update_error_label.set_text(t("No online updates to import."))
            return

        activate_identities = [
            identity
            for identity, checkbox in online_update_activate_checkboxes.items()
            if bool(getattr(checkbox, "value", False))
        ]

        try:
            result = service.import_online_updates(
                updates=pending_online_updates,
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

        # Re-index immediately only when every imported update was activated.
        # Otherwise keep imported-but-not-activated rows visible as inactive.
        if activated > 0 and activated == imported:
            perform_index("online updates")
        else:
            refresh_data()

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

    def restart_klipper() -> None:
        """Request Klipper restart when macro changes are pending and printer is idle."""
        if not restart_required:
            status_label.set_text(t("No pending macro changes require a Klipper restart."))
            return
        if printer_is_printing or printer_is_busy:
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
        if not dynamic_reload_required:
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

    def set_print_lock(locked: bool, moonraker_state: str, moonraker_message: str) -> None:
        """Toggle UI mutation lock while printer is actively printing."""
        nonlocal printer_is_printing
        nonlocal printer_is_busy
        nonlocal printer_state
        nonlocal print_lock_popup_open

        printer_is_printing = locked
        printer_state = moonraker_state
        printer_is_busy = moonraker_state not in {"standby", "ready", "complete", "cancelled"}
        selected_macro_dynamic = False
        if selected_key:
            for macro in cached_macros:
                if macro_key(macro) == selected_key:
                    selected_macro_dynamic = bool(macro.get("is_dynamic", False))
                    break
        editing_enabled = ((not locked) or selected_macro_dynamic) and _remote_actions_available()

        index_button.set_enabled(editing_enabled)
        backup_button.set_enabled(editing_enabled)
        macro_actions_button.set_enabled(editing_enabled)
        duplicate_warning_button.set_enabled(editing_enabled)
        purge_deleted_button.set_enabled(editing_enabled and deleted_macro_count > 0)
        create_backup_button.set_enabled(editing_enabled)
        confirm_export_button.set_enabled(editing_enabled)
        confirm_import_button.set_enabled(editing_enabled)
        confirm_create_pr_button.set_enabled(editing_enabled)
        confirm_online_update_button.set_enabled(editing_enabled and bool(pending_online_updates))
        confirm_restore_button.set_enabled(editing_enabled)
        confirm_delete_button.set_enabled(editing_enabled)
        duplicate_compare_button.set_enabled(editing_enabled)
        duplicate_prev_button.set_enabled(editing_enabled and duplicate_wizard_index > 0)
        duplicate_next_button.set_enabled(editing_enabled and duplicate_wizard_index < len(duplicate_wizard_groups) - 1)
        duplicate_apply_button.set_enabled(editing_enabled)
        viewer.set_editing_enabled(editing_enabled)
        _refresh_reload_buttons()

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
        if remote_mode_enabled and not remote_api_connected:
            set_print_lock(
                locked=False,
                moonraker_state="unknown",
                moonraker_message=t("Remote API disconnected."),
            )
            return
        try:
            status = service.query_printer_status(timeout=1.5)
        except Exception as exc:
            if remote_mode_enabled:
                _set_remote_connection_state(False, str(exc))
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

    def refresh_remote_health() -> None:
        """Poll host API health in remote mode and update connection indicators."""
        if not remote_mode_enabled:
            return
        try:
            health = service.query_health()
            last_index_at = _to_optional_int(health.get("last_index_at"))
            if last_index_at is None:
                detail = t("last scan unknown")
            else:
                detail = t("last scan {value}", value=_format_ts(last_index_at))
            _set_remote_connection_state(True, detail)
        except Exception as exc:
            _set_remote_connection_state(False, str(exc))

    def _enqueue_remote_event(event: dict[str, object]) -> None:
        """Push one remote SSE event into the UI-safe processing queue."""
        nonlocal remote_last_event_id
        event_id = _to_int(event.get("id", 0), default=0)
        if event_id > 0:
            remote_last_event_id = max(remote_last_event_id, event_id)
        remote_event_queue.put(event)

    def _start_remote_event_listener() -> None:
        """Start background SSE listener for remote mode updates."""
        nonlocal remote_event_listener_thread
        if not remote_mode_enabled:
            return
        if remote_event_listener_thread is not None and remote_event_listener_thread.is_alive():
            return

        remote_event_stop.clear()

        def _worker() -> None:
            try:
                service.stream_events(
                    on_event=_enqueue_remote_event,
                    stop_requested=remote_event_stop.is_set,
                    last_event_id=remote_last_event_id,
                )
            except Exception:
                # Health polling handles disconnected-state messaging.
                return

        remote_event_listener_thread = threading.Thread(
            target=_worker,
            name="klippervault-remote-events",
            daemon=True,
        )
        remote_event_listener_thread.start()

    def _drain_remote_events() -> None:
        """Drain queued remote events on the UI thread and trigger refreshes."""
        nonlocal remote_data_dirty
        if not remote_mode_enabled:
            return

        processed_any = False
        while True:
            try:
                event = remote_event_queue.get_nowait()
            except Empty:
                break

            processed_any = True
            event_type = str(event.get("type", "")).strip()
            if event_type:
                _set_remote_connection_state(True)

            if event_type in {
                "index.completed",
                "job.completed",
                "action.completed",
            }:
                remote_data_dirty = True

        if processed_any and remote_data_dirty and (not is_indexing) and remote_api_connected:
            remote_data_dirty = False
            refresh_data()

    def check_config_changes() -> None:
        """Timer callback: auto-rescan when cfg files change."""
        nonlocal no_clients_since
        nonlocal no_client_cache_released
        nonlocal remote_data_dirty

        if memory_trim_no_clients_enabled and not _is_any_client_connected():
            if no_clients_since is None:
                no_clients_since = time.monotonic()
            if not no_client_cache_released:
                _release_ui_caches_for_no_clients()
                no_client_cache_released = True
            if (time.monotonic() - no_clients_since) >= memory_trim_no_clients_grace_seconds:
                _trim_process_memory("no-clients")
            return

        no_clients_since = None
        no_client_cache_released = False
        refresh_remote_health()
        refresh_print_state()
        if remote_mode_enabled:
            if not remote_api_connected:
                status_label.set_text(t("Remote API disconnected. Showing cached data in read-only mode."))
                return
            if remote_data_dirty and (not is_indexing):
                remote_data_dirty = False
                refresh_data()
                return
            if not is_indexing:
                refresh_data()
            return
        if printer_is_printing:
            _trim_process_memory("printing")
            return
        if is_indexing:
            return
        changed = watcher.poll_changed()
        if changed:
            _note_activity()
            perform_index("watcher")
            return

        # Treat printer standby/ready/complete/cancelled as idle-capable state.
        if (not printer_is_busy) and ((time.monotonic() - last_activity_monotonic) >= memory_trim_idle_seconds):
            _trim_process_memory("idle")

    def toggle_duplicates_filter() -> None:
        """Toggle duplicate-only filter and rerender list."""
        nonlocal show_duplicates_only
        show_duplicates_only = not show_duplicates_only
        update_duplicates_button_label()
        render_macro_list()

    def _go_prev_page() -> None:
        """Navigate one macro-list page backward and refresh data."""
        nonlocal list_page_index
        if list_page_index <= 0:
            return
        list_page_index -= 1
        refresh_data()

    def _go_next_page() -> None:
        """Navigate one macro-list page forward and refresh data."""
        nonlocal list_page_index
        total_pages = max(1, (max(total_macro_rows, 1) + list_page_size - 1) // list_page_size)
        if (list_page_index + 1) >= total_pages:
            return
        list_page_index += 1
        refresh_data()

    def toggle_new_filter() -> None:
        """Toggle new-only filter and rerender list."""
        nonlocal show_new_only
        show_new_only = not show_new_only
        update_new_button_label()
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
        """Search input change handler — updates query and marks list as dirty."""
        nonlocal search_query, _search_dirty
        search_query = e.value or ""
        _search_dirty = True

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
    search_input.on_value_change(on_search_change)
    duplicate_warning_button.on_click(open_duplicate_wizard)
    backup_button.on_click(open_backup_dialog)
    with macro_actions_menu:
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
    prev_page_button.on_click(_go_prev_page)
    next_page_button.on_click(_go_next_page)

    if _printer_profile_missing():
        printer_vendor_input.set_value(str(vault_cfg.printer_vendor or "").strip())
        printer_model_input.set_value(str(vault_cfg.printer_model or "").strip())
        printer_profile_dialog.open()

    if remote_mode_enabled:
        refresh_remote_health()
        _start_remote_event_listener()
    refresh_print_state()
    if remote_mode_enabled:
        if remote_api_connected:
            refresh_data()
        else:
            status_label.set_text(t("Remote API disconnected. Showing cached data in read-only mode."))
    elif not printer_is_printing:
        perform_index("startup")
    else:
        refresh_data()
    ui.timer(0.5, lambda: asyncio.create_task(_check_online_updates_on_startup()), once=True)
    watcher.reset()
    ui.timer(0.5, _refresh_create_pr_progress_ui)
    ui.timer(0.5, _refresh_online_update_progress_ui)
    ui.timer(0.25, _drain_remote_events)
    ui.timer(2.0, check_config_changes)

    def _flush_search() -> None:
        nonlocal _search_dirty
        if _search_dirty:
            _search_dirty = False
            render_macro_list()

    ui.timer(0.25, _flush_search)

    with ui.footer().classes("items-center justify-end px-4 py-1 bg-grey-9 text-grey-3"):
        ui.label(f"KlipperVault v{app_version}").classes("text-xs")
