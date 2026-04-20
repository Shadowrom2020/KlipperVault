#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reusable macro preview/editor component for KlipperVault GUI."""

from __future__ import annotations

import json
from typing import Callable

from nicegui import ui
from klipper_vault_i18n import t


class MacroEditor:
    """Render macro preview and manage in-place editing state."""

    def __init__(self) -> None:
        self._current_macro: dict | None = None
        self._current_preview_text = ""
        self._current_section_text = ""
        self._save_handler: Callable[[dict, str], None] | None = None
        self._editing_enabled = True
        self._editor_open = False
        self._editable = False
        self._explain_handler: Callable[[], None] | None = None
        self._delete_handler: Callable[[dict], None] | None = None
        self._restore_handler: Callable[[dict], None] | None = None

        with ui.row().classes("w-full items-center gap-2 mt-2 px-3 py-2 bg-grey-8 text-grey-2 rounded-t"):
            ui.label(t("Macro preview")).classes("text-sm font-medium flex-1")
            self._explain_button = ui.button(icon="help_outline", on_click=self._do_explain).props("flat round dense")
            self._explain_button.tooltip(t("Explain this macro"))
            self._explain_button.set_visibility(False)
            self._restore_button = ui.button(icon="restore_page", on_click=self._restore_macro).props("flat round dense")
            self._restore_button.classes("text-orange-5")
            self._restore_button.tooltip(t("Restore deleted macro"))
            self._restore_button.set_visibility(False)
            self._delete_button = ui.button(icon="delete_forever", on_click=self._delete_macro).props("flat round dense")
            self._delete_button.classes("text-negative")
            self._delete_button.tooltip(t("Delete macro from cfg file"))
            self._delete_button.set_visibility(False)
            self._edit_button = ui.button(icon="edit", on_click=self._start_editing).props("flat round dense")
            self._edit_button.classes("text-primary")
            self._edit_button.tooltip(t("Edit macro"))
            self._edit_button.set_visibility(False)
            self._save_edit_button = ui.button(icon="save", on_click=self._save_macro_edit).props("flat round dense")
            self._save_edit_button.classes("text-positive")
            self._save_edit_button.tooltip(t("Save changes"))
            self._save_edit_button.set_visibility(False)
            self._cancel_edit_button = ui.button(icon="close", on_click=self.close_editor).props("flat round dense")
            self._cancel_edit_button.tooltip(t("Cancel editing"))
            self._cancel_edit_button.set_visibility(False)
        self._code_view = ui.code("", language="ini").classes("w-full")
        self._editor = self._create_editor()
        self._editor.set_visibility(False)
        self._edit_status_label = ui.label("").classes("text-sm text-negative mt-2")
        self._edit_status_label.set_visibility(False)

    def _create_editor(self):
        """Create syntax-highlighted editor with fallback for older NiceGUI builds."""
        codemirror_factory = getattr(ui, "codemirror", None)
        if callable(codemirror_factory):
            return codemirror_factory(
                "",
                language="Properties",
                theme="basicDark",
                line_wrapping=True,
            ).classes("w-full h-[28rem] border border-grey-4 rounded-b")

        return ui.textarea(label=t("Macro source")).props(
            "outlined autogrow input-style=font-family:monospace"
        ).classes("w-full")

    def _set_editor_value(self, text: str) -> None:
        """Update editor content across NiceGUI editor implementations."""
        if hasattr(self._editor, "set_value"):
            self._editor.set_value(text)
        else:
            self._editor.value = text
            self._editor.update()

    def _get_editor_value(self) -> str:
        """Read editor content across NiceGUI editor implementations."""
        return str(getattr(self._editor, "value", "") or "")

    def _set_code(self, code_text: str) -> None:
        """Set macro-section code text on ui.code across NiceGUI versions."""
        if hasattr(self._code_view, "set_content"):
            self._code_view.set_content(code_text)
            return
        if hasattr(self._code_view, "content"):
            self._code_view.content = code_text
            self._code_view.update()
            return
        self._code_view.update()

    def _build_macro_section_text(self, macro: dict | None) -> str:
        """Build editor text for the selected macro row."""
        if macro is None:
            return ""
        description = str(macro.get("description") or "-")
        rename_existing = str(macro.get("rename_existing") or "").strip()
        gcode_text = str(macro.get("gcode") or "")
        try:
            variables = json.loads(str(macro.get("variables_json") or "{}"))
        except (TypeError, json.JSONDecodeError):
            variables = {}

        macro_lines = [f"[gcode_macro {macro.get('macro_name', '')}]"]
        if description != "-":
            macro_lines.append(f"description: {description}")
        if rename_existing:
            macro_lines.append(f"rename_existing: {rename_existing}")
        if isinstance(variables, dict):
            for key in sorted(variables.keys()):
                macro_lines.append(f"variable_{key}: {variables[key]}")
        if gcode_text:
            macro_lines.append("gcode:")
            for line in gcode_text.splitlines():
                macro_lines.append(f"{line}")
        return "\n".join(macro_lines) + "\n"

    def _show_edit_error(self, error: Exception) -> None:
        """Display one editor error message consistently."""
        self._edit_status_label.set_text(str(error))
        self._edit_status_label.set_visibility(True)

    def _set_edit_mode(self, editing: bool) -> None:
        """Switch between read-only code view and editable text area."""
        self._editor_open = editing
        self._code_view.set_visibility(not editing)
        self._editor.set_visibility(editing)
        self._save_edit_button.set_visibility(editing)
        self._cancel_edit_button.set_visibility(editing)
        self._edit_button.set_visibility((not editing) and self._editable)
        self._delete_button.set_visibility((not editing) and self._editable and self._delete_handler is not None)
        self._restore_button.set_visibility((not editing) and self._can_restore_current_macro())
        self._explain_button.set_visibility((not editing) and (self._current_macro is not None))
        if not editing:
            self._edit_status_label.set_visibility(False)

        self.set_editing_enabled(self._editing_enabled)

    def _can_restore_current_macro(self) -> bool:
        """Return True when current macro can be restored from deleted state."""
        return (
            self._current_macro is not None
            and bool(self._current_macro.get("is_deleted", False))
            and self._restore_handler is not None
        )

    def _start_editing(self) -> None:
        """Open the in-place editor for the current macro."""
        if not self._editing_enabled or not self._editable or self._current_macro is None:
            return
        self._set_editor_value(self._current_section_text)
        self._edit_status_label.set_visibility(False)
        self._set_edit_mode(True)

    def _save_macro_edit(self) -> None:
        """Invoke callback to save edited macro text."""
        if self._save_handler is None or self._current_macro is None:
            return
        try:
            self._save_handler(self._current_macro, self._get_editor_value())
        except Exception as exc:
            self._show_edit_error(exc)
            return

        self._set_edit_mode(False)

    def set_save_handler(self, handler: Callable[[dict, str], None] | None) -> None:
        """Register callback used by the in-place macro editor save action."""
        self._save_handler = handler

    def set_explain_handler(self, handler: Callable[[], None] | None) -> None:
        """Register callback invoked when the explain button is clicked."""
        self._explain_handler = handler

    def set_delete_handler(self, handler: Callable[[dict], None] | None) -> None:
        """Register callback invoked when deleting selected macro from cfg."""
        self._delete_handler = handler

    def set_restore_handler(self, handler: Callable[[dict], None] | None) -> None:
        """Register callback invoked when restoring selected deleted macro."""
        self._restore_handler = handler

    def _do_explain(self) -> None:
        """Invoke the explain handler if one is registered."""
        if self._explain_handler is not None and self._current_macro is not None:
            self._explain_handler()

    def _delete_macro(self) -> None:
        """Invoke callback to delete selected macro section from cfg file."""
        if self._delete_handler is None or self._current_macro is None:
            return
        try:
            self._delete_handler(self._current_macro)
        except Exception as exc:
            self._show_edit_error(exc)

    def _restore_macro(self) -> None:
        """Invoke callback to restore selected deleted macro."""
        if self._restore_handler is None or self._current_macro is None:
            return
        try:
            self._restore_handler(self._current_macro)
        except Exception as exc:
            self._show_edit_error(exc)

    def set_editing_enabled(self, enabled: bool) -> None:
        """Enable or disable mutating actions while keeping read-only view active."""
        self._editing_enabled = enabled
        if enabled:
            self._edit_button.enable()
            self._delete_button.enable()
            self._restore_button.enable()
            self._save_edit_button.enable()
            self._cancel_edit_button.enable()
            self._editor.enable()
        else:
            self._edit_button.disable()
            self._delete_button.disable()
            self._restore_button.disable()
            self._save_edit_button.disable()
            self._cancel_edit_button.disable()
            self._editor.disable()

    def close_editor(self) -> None:
        """Exit edit mode without saving."""
        self._set_edit_mode(False)

    def show_macro(self, macro: dict | None, preview_text: str, *, editable: bool) -> None:
        """Render macro preview and sync editor state for current selection."""
        self._current_macro = macro
        self._current_preview_text = preview_text
        self._current_section_text = self._build_macro_section_text(macro)
        self._editable = editable

        if macro is None:
            self._set_code("")
            self._set_edit_mode(False)
            return

        self._set_code(self._current_preview_text)
        if self._editor_open:
            self._set_editor_value(self._current_section_text)
        self._edit_button.set_visibility(self._editable and not self._editor_open)
        self._delete_button.set_visibility(self._editable and not self._editor_open and self._delete_handler is not None)
        self._restore_button.set_visibility(not self._editor_open and self._can_restore_current_macro())
        self._explain_button.set_visibility(not self._editor_open)
