#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Extracted UI component builders for NiceGUI interface."""

from __future__ import annotations

from nicegui import ui

from klipper_vault_i18n import t
from klipper_macro_gui_state import UIState


def build_toolbar(state: UIState) -> None:
    """Build the top toolbar with action buttons and menus.
    
    Creates and wires the toolbar header with macro actions, optional developer menu,
    reload/restart buttons, duplicate warning, and scan/backup buttons.
    Stores references to all created elements in the state object for later access.
    """
    with ui.header().classes("items-center gap-2 px-4 py-2 bg-grey-9 flex-wrap") as toolbar_header:
        state.toolbar_header = toolbar_header
        
        ui.label(t("Klipper Vault")).classes("text-xl font-bold text-white")
        ui.space()
        
        # Macro actions button and menu
        with ui.button(t("Macro actions"), icon="menu").props("flat color=white") as macro_actions_button:
            state.macro_actions_button = macro_actions_button
            with ui.menu() as macro_actions_menu:
                state.macro_actions_menu = macro_actions_menu
                # Menu items will be populated by build_ui() caller via macro_actions_menu
        
        # Developer menu (optional)
        state.developer_menu = None
        # Developer menu will be created conditionally by caller if enabled
        
        # Reload dynamic macros button
        reload_dynamic_macros_button = ui.button(t("Reload Dynamic Macros"), icon="autorenew").props("flat color=white")
        reload_dynamic_macros_button.classes("text-blue-4")
        reload_dynamic_macros_button.set_visibility(False)
        state.reload_dynamic_macros_button = reload_dynamic_macros_button
        
        # Restart Klipper button
        restart_klipper_button = ui.button(t("Restart Klipper"), icon="restart_alt").props("flat color=white")
        restart_klipper_button.classes("text-orange-4")
        restart_klipper_button.set_visibility(False)
        state.restart_klipper_button = restart_klipper_button
        
        # Duplicate warning button
        duplicate_warning_button = ui.button(t("Duplicates found"), icon="warning").props("flat no-caps")
        duplicate_warning_button.classes("text-yellow-5")
        duplicate_warning_button.set_visibility(False)
        state.duplicate_warning_button = duplicate_warning_button
        
        # Backup button
        backup_button = ui.button(t("Backup"), icon="save").props("flat color=white")
        state.backup_button = backup_button
        
        # Scan/Index button
        index_button = ui.button(t("Scan macros"), icon="search").props("flat color=white")
        state.index_button = index_button
