#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Macro viewer panel for KlipperVault GUI."""

from __future__ import annotations

from typing import Callable

from nicegui import ui
from klipper_vault_i18n import t

# Re-export format_ts so callers (e.g. klipper_macro_gui) can import it from here.
from klipper_macro_compare import format_ts, MacroCompareView
from klipper_macro_editor import MacroEditor
from klipper_macro_explainer_view import MacroExplainerView


class MacroViewer:
    """Builds the center macro viewer card and manages its displayed state."""

    def __init__(self) -> None:
        self._current_key: str | None = None
        self._current_macro: dict | None = None
        self._all_versions: dict[int, dict] = {}
        self._active_macro: dict | None = None
        self._open_macro_handler: Callable[[str, str], None] | None = None
        self._remove_deleted_handler: Callable[[str, str], None] | None = None
        self._remove_inactive_handler: Callable[[dict], None] | None = None
        self._restore_version_handler: Callable[[dict], None] | None = None
        self._delete_macro_from_cfg_handler: Callable[[dict], None] | None = None
        self._compare_view = MacroCompareView()
        self._editing_enabled = True
        self._available_macros: list[dict[str, object]] = []

        with ui.card().classes("col-span-2 h-full overflow-y-auto"):
            ui.label(t("Macro viewer")).classes("text-lg font-semibold mb-2")
            with ui.row().classes("w-full items-center gap-3"):
                self._name_label = ui.label(t("No macro selected")).classes("text-xl font-bold flex-1")
                self._active_star_label = ui.label("★").classes("text-xl text-positive")
                self._active_star_label.set_visibility(False)
                self._version_select = (
                    ui.select(
                        options={},
                        label=t("Version"),
                        on_change=lambda e: self._show_version(e.value),
                    )
                    .props("dense outlined")
                    .classes("w-44")
                )
                self._compare_button = ui.button(t("Compare"), on_click=self._compare_view.open).props(
                    "flat no-caps"
                )
                self._compare_button.disable()
                self._compare_button.set_visibility(False)
                self._remove_deleted_button = ui.button(
                    t("Remove deleted"),
                    on_click=self._remove_deleted_macro,
                ).props("flat no-caps")
                self._remove_deleted_button.classes("text-negative")
                self._remove_deleted_button.set_visibility(False)
                self._remove_inactive_button = ui.button(
                    t("Remove inactive version"),
                    on_click=self._remove_inactive_macro,
                ).props("flat no-caps")
                self._remove_inactive_button.classes("text-negative")
                self._remove_inactive_button.set_visibility(False)
                self._restore_version_button = ui.button(
                    t("Revert"),
                    on_click=self._restore_selected_version,
                ).props("flat no-caps")
                self._restore_version_button.classes("text-orange-5")
                self._restore_version_button.set_visibility(False)
            self._meta_label = ui.label(t("Choose a macro from the list.")).classes("text-sm text-grey-4 mt-1")
            with ui.row().classes("w-full items-center gap-2 mt-1"):
                self._rename_hint = ui.label("").classes("text-sm text-blue-4")
                self._inactive_hint = ui.label("").classes("text-sm text-yellow-7")
                self._open_active_button = ui.button(
                    t("Open active macro"),
                    on_click=self._open_active_macro,
                ).props("flat dense no-caps")
                self._open_active_button.classes("text-yellow-5")
                self._rename_hint.set_visibility(False)
                self._inactive_hint.set_visibility(False)
                self._open_active_button.set_visibility(False)
            self._desc_label = ui.label(t("Description: -")).classes("text-sm mt-2")
            self._editor_panel = MacroEditor()
            self._editor_panel.set_explain_handler(self._open_explainer)
        self._explainer_panel = MacroExplainerView()

    def _open_explainer(self) -> None:
        """Open the on-demand explanation dialog for the current macro."""
        if self._current_macro is None:
            return
        self._explainer_panel.open()

    def _show_version(self, version_num: int | None) -> None:
        """Show one selected version in the viewer."""
        if version_num is None:
            self._show_content(None)
            return
        self._show_content(self._all_versions.get(int(version_num)))

    def _open_active_macro(self) -> None:
        """Navigate to the active macro counterpart when available."""
        if self._active_macro is None or self._open_macro_handler is None:
            return
        self._open_macro_handler(
            str(self._active_macro.get("file_path", "")),
            str(self._active_macro.get("macro_name", "")),
        )

    def set_open_macro_handler(self, handler: Callable[[str, str], None] | None) -> None:
        """Register callback used by the "Open active macro" action."""
        self._open_macro_handler = handler
        self._explainer_panel.set_open_macro_handler(handler)

    def set_available_macros(self, macros: list[dict[str, object]]) -> None:
        """Update macro index used for explanation cross-links."""
        self._available_macros = list(macros)
        self._explainer_panel.set_available_macros(self._available_macros)

    def set_remove_deleted_handler(self, handler: Callable[[str, str], None] | None) -> None:
        """Register callback used by the "Remove deleted" action."""
        self._remove_deleted_handler = handler

    def set_remove_inactive_handler(self, handler: Callable[[dict], None] | None) -> None:
        """Register callback used by the "Remove inactive version" action."""
        self._remove_inactive_handler = handler

    def set_restore_version_handler(self, handler: Callable[[dict], None] | None) -> None:
        """Register callback used by the "Revert/Restore" action."""
        self._restore_version_handler = handler
        self._editor_panel.set_restore_handler(handler)

    def set_save_macro_edit_handler(self, handler: Callable[[dict, str], None] | None) -> None:
        """Register callback used by the in-place macro editor save action."""
        self._editor_panel.set_save_handler(handler)

    def set_delete_macro_from_cfg_handler(self, handler: Callable[[dict], None] | None) -> None:
        """Register callback used by the preview toolbar delete-from-cfg action."""
        self._delete_macro_from_cfg_handler = handler
        self._editor_panel.set_delete_handler(handler)

    def set_editing_enabled(self, enabled: bool) -> None:
        """Enable or disable mutating actions while keeping read-only view active."""
        self._editing_enabled = enabled
        if enabled:
            self._remove_deleted_button.enable()
            self._remove_inactive_button.enable()
            self._restore_version_button.enable()
            self._open_active_button.enable()
        else:
            self._remove_deleted_button.disable()
            self._remove_inactive_button.disable()
            self._restore_version_button.disable()
            self._open_active_button.disable()
        self._editor_panel.set_editing_enabled(enabled)

    def _is_latest_version_selected(self, macro: dict | None) -> bool:
        """Return True when the current selection is the latest stored version."""
        if macro is None:
            return False
        latest_version = max(self._all_versions.keys()) if self._all_versions else int(macro.get("version", 0))
        return int(macro.get("version", 0)) == latest_version

    def _can_edit_macro(self, macro: dict | None) -> bool:
        """Return True when selected macro is editable in-place."""
        return (
            macro is not None
            and not macro.get("is_deleted", False)
            and self._is_latest_version_selected(macro)
        )

    def _remove_deleted_macro(self) -> None:
        """Invoke callback to purge selected deleted macro from DB."""
        if self._remove_deleted_handler is None or self._current_macro is None:
            return
        if not self._current_macro.get("is_deleted", False):
            return
        self._remove_deleted_handler(
            str(self._current_macro.get("file_path", "")),
            str(self._current_macro.get("macro_name", "")),
        )

    def _remove_inactive_macro(self) -> None:
        """Invoke callback to purge selected inactive macro version from DB."""
        if self._remove_inactive_handler is None or self._current_macro is None:
            return
        if self._current_macro.get("is_active", False) or self._current_macro.get("is_deleted", False):
            return
        self._remove_inactive_handler(self._current_macro)

    def _restore_selected_version(self) -> None:
        """Invoke callback to restore selected version to cfg file."""
        if self._restore_version_handler is None or self._current_macro is None:
            return
        self._restore_version_handler(self._current_macro)

    def _is_macro_currently_deleted(self) -> bool:
        """Return deletion status of the latest version for current macro identity."""
        if not self._all_versions:
            return False
        latest_version = max(self._all_versions.keys())
        latest_macro = self._all_versions.get(latest_version)
        return bool(latest_macro and latest_macro.get("is_deleted", False))

    def _update_restore_button(self, macro: dict | None) -> None:
        """Show revert/restore action only for old versions or deleted macros."""
        if macro is None:
            self._restore_version_button.set_visibility(False)
            return

        latest_version = max(self._all_versions.keys()) if self._all_versions else int(macro.get("version", 0))
        selected_version = int(macro.get("version", 0))
        is_deleted = bool(macro.get("is_deleted", False))
        is_new = bool(macro.get("is_new", False))
        is_older_version = selected_version < latest_version
        is_currently_deleted = self._is_macro_currently_deleted()
        can_restore_deleted = is_currently_deleted and is_deleted
        can_restore_new = is_new and (not is_deleted)

        if not (can_restore_deleted or is_older_version or can_restore_new):
            self._restore_version_button.set_visibility(False)
            return

        if can_restore_deleted:
            self._restore_version_button.set_text(t("Restore deleted macro"))
        elif can_restore_new:
            self._restore_version_button.set_text(t("Enable imported macro"))
        else:
            self._restore_version_button.set_text(t("Revert to this version"))
        self._restore_version_button.set_visibility(True)

    def _version_status_label(self, version_row: dict) -> str:
        """Return compact status label for one version option."""
        if version_row.get("is_deleted", False):
            return t("DELETED")
        if version_row.get("is_new", False):
            return t("NEW")
        if version_row.get("is_active", False):
            return "★"
        return t("inactive")

    def _update_inactive_hint(self, macro: dict | None) -> None:
        """Update inactive explanation line and optional jump button."""
        if macro is None or macro.get("is_active", False):
            self._inactive_hint.set_visibility(False)
            self._open_active_button.set_visibility(False)
            return

        if bool(macro.get("is_deleted", False)):
            self._inactive_hint.set_text(
                t("Deleted: this macro no longer exists in the cfg files. It is stored in the vault until removed.")
            )
            self._inactive_hint.set_visibility(True)
            self._open_active_button.set_visibility(False)
            return

        if not bool(macro.get("is_loaded", True)):
            self._inactive_hint.set_text(
                t("Inactive: this macro is defined in a cfg file that is not currently loaded.")
            )
            self._inactive_hint.set_visibility(True)
            self._open_active_button.set_visibility(False)
            return

        # Historical versions are expected to be inactive; only show override hints
        # when viewing the latest version for this macro identity.
        selected_version = int(macro.get("version", 0))
        latest_version = max(self._all_versions.keys()) if self._all_versions else selected_version
        if selected_version < latest_version:
            self._inactive_hint.set_visibility(False)
            self._open_active_button.set_visibility(False)
            return

        if self._active_macro is None:
            self._inactive_hint.set_text(
                t("Inactive: this macro is overridden by another definition loaded later.")
            )
            self._inactive_hint.set_visibility(True)
            self._open_active_button.set_visibility(False)
            return

        active_path = str(self._active_macro.get("file_path", "-"))
        self._inactive_hint.set_text(
            t("Inactive: overridden by active definition in {path}.", path=active_path)
        )
        self._open_active_button.set_text(t("Open active macro"))
        self._inactive_hint.set_visibility(True)
        self._open_active_button.set_visibility(True)

    def _update_rename_hint(self, macro: dict | None) -> None:
        """Show why a macro appears under a renamed runtime command."""
        if macro is None:
            self._rename_hint.set_visibility(False)
            return

        renamed_from = str(macro.get("renamed_from") or "").strip()
        display_name = str(macro.get("display_name") or macro.get("macro_name") or "").strip()
        if not renamed_from or not display_name:
            self._rename_hint.set_visibility(False)
            return

        self._rename_hint.set_text(
            t(
                "Renamed from {source} to {target} via rename_existing in a later macro definition.",
                source=renamed_from,
                target=display_name,
            )
        )
        self._rename_hint.set_visibility(True)

    def _show_content(self, macro: dict | None) -> None:
        """Render selected macro details into all viewer widgets."""
        self._current_macro = macro
        if macro is None:
            self._meta_label.set_text(t("Choose a macro from the list."))
            self._active_star_label.set_visibility(False)
            self._rename_hint.set_visibility(False)
            self._inactive_hint.set_visibility(False)
            self._open_active_button.set_visibility(False)
            self._desc_label.set_text(t("Description: -"))
            self._remove_deleted_button.set_visibility(False)
            self._remove_inactive_button.set_visibility(False)
            self._restore_version_button.set_visibility(False)
            self._editor_panel.show_macro(None, "", editable=False)
            self._explainer_panel.set_macro(None)
            return

        is_active = bool(macro.get("is_active", False))
        self._active_star_label.set_visibility(is_active)

        self._meta_label.set_text(
            f"{macro.get('file_path', '-')}, line {macro.get('line_number', '-')}, "
            f"indexed {format_ts(int(macro.get('indexed_at', 0)))}, "
            f"{'DELETED' if bool(macro.get('is_deleted', False)) else (t('not_loaded') if not bool(macro.get('is_loaded', True)) else ('★' if is_active else 'inactive'))}"
        )
        # Only allow purge when the macro identity is currently deleted (latest version is deleted).
        self._remove_deleted_button.set_visibility(self._is_macro_currently_deleted())
        self._remove_inactive_button.set_visibility(
            (not bool(macro.get("is_active", False)))
            and (not bool(macro.get("is_deleted", False)))
        )
        self._update_restore_button(macro)
        self._update_rename_hint(macro)
        self._update_inactive_hint(macro)
        description = str(macro.get("description") or "-")
        rename_existing = str(macro.get("rename_existing") or "").strip()
        self._desc_label.set_text(t("Description: {description}", description=description))
        gcode_text = str(macro.get("gcode") or "")

        macro_lines = [f"[gcode_macro {macro.get('macro_name', '')}]"]
        if description != "-":
            macro_lines.append(f"description: {description}")
        if rename_existing:
            macro_lines.append(f"rename_existing: {rename_existing}")
        if gcode_text:
            macro_lines.append("gcode:")
            for line in gcode_text.splitlines():
                macro_lines.append(f"  {line}")
        self._editor_panel.show_macro(
            macro,
            "\n".join(macro_lines),
            editable=self._can_edit_macro(macro),
        )
        self._explainer_panel.set_macro(macro)

    def set_macro(
        self,
        macro: dict | None,
        versions: list[dict],
        active_macro: dict | None = None,
        *,
        prefer_latest: bool = False,
        prefer_active: bool = False,
    ) -> None:
        """Update viewer when a macro is selected in the list."""
        if macro is None:
            self._current_key = None
            self._current_macro = None
            self._all_versions = {}
            self._active_macro = None
            self._name_label.set_text(t("No macro selected"))
            self._version_select.options = {}
            self._version_select.value = None
            self._version_select.update()
            self._compare_button.disable()
            self._compare_button.set_visibility(False)
            self._remove_inactive_button.set_visibility(False)
            self._restore_version_button.set_visibility(False)
            self._compare_view.set_macro(None, [])
            self._show_content(None)
            return

        new_key = f"{macro['file_path']}::{macro['macro_name']}"
        self._current_macro = macro
        self._all_versions = {int(v["version"]): v for v in versions}
        self._active_macro = active_macro
        options = {
            int(v["version"]): (
                f"{format_ts(int(v['indexed_at']))}  {self._version_status_label(v)}"
            )
            for v in versions
        }
        display_name = str(macro.get("display_name") or macro.get("macro_name", ""))
        self._name_label.set_text(display_name)
        self._version_select.options = options
        self._version_select.update()
        if len(versions) >= 2:
            self._compare_button.enable()
            self._compare_button.set_visibility(True)
        else:
            self._compare_button.disable()
            self._compare_button.set_visibility(False)
        self._compare_view.set_macro(macro, versions)

        if new_key != self._current_key or prefer_latest or prefer_active:
            self._editor_panel.close_editor()
            self._current_key = new_key
            selected_version: int | None = None
            if prefer_active:
                active_version = next(
                    (int(v["version"]) for v in versions if bool(v.get("is_active", False))),
                    None,
                )
                selected_version = active_version
            if selected_version is None:
                selected_version = int(versions[0]["version"]) if versions else None

            self._version_select.value = selected_version
            self._version_select.update()
            self._show_version(selected_version)
        else:
            self._show_version(self._version_select.value)
