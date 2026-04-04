#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reusable explanation panel for Klipper macro scripts."""

from __future__ import annotations

from typing import Callable

from nicegui import ui

from klipper_macro_explainer import explain_macro_script
from klipper_vault_i18n import t


def _as_dict_list(value: object) -> list[dict[str, object]]:
    """Normalize dynamic explain payload entries into list[dict[str, object]]."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


class MacroExplainerView:
    """Render user-facing explanations for the selected macro."""

    def __init__(self) -> None:
        self._current_macro: dict[str, object] | None = None
        self._available_macros: list[dict[str, object]] = []
        self._open_macro_handler: Callable[[str, str], None] | None = None
        self._selected_reference: dict[str, object] | None = None

        with ui.dialog() as self._dialog, ui.card().classes("w-[74rem] max-w-[98vw] h-[86vh] max-h-[94vh] flex flex-col"):
            ui.label(t("Script explanation")).classes("text-md font-semibold")
            ui.label(
                t("Disclaimer: Macro explanation is an early development feature and may be inaccurate.")
            ).classes("text-xs text-yellow-4")
            self._summary_label = ui.label(t("Select a macro to explain its g-code.")).classes("text-sm text-grey-4")
            ui.label(t("Referenced macros")).classes("text-sm font-medium mt-2")
            self._references_row = ui.row().classes("w-full gap-2 items-center")
            self._references_empty = ui.label(t("No referenced macros detected.")).classes("text-xs text-grey-5")
            ui.separator().classes("my-2")
            self._lines_column = ui.column().classes("w-full gap-2 flex-1 overflow-y-auto")
            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button(t("Close"), on_click=self._dialog.close).props("flat no-caps")

        with ui.dialog() as self._reference_dialog, ui.card().classes("w-[34rem] max-w-[96vw]"):
            self._reference_title = ui.label(t("Referenced macro")).classes("text-lg font-semibold")
            self._reference_subtitle = ui.label("").classes("text-sm text-grey-5")
            self._reference_status = ui.label("").classes("text-sm mt-2")
            self._reference_note = ui.label("").classes("text-sm text-grey-4 mt-1")
            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button(t("Close"), on_click=self._reference_dialog.close).props("flat no-caps")
                self._reference_open_button = ui.button(t("Open macro"), on_click=self._open_selected_reference).props(
                    "color=primary no-caps"
                )

    def set_open_macro_handler(self, handler: Callable[[str, str], None] | None) -> None:
        """Register callback used to navigate to another macro."""
        self._open_macro_handler = handler

    def set_available_macros(self, macros: list[dict[str, object]]) -> None:
        """Update macro reference universe used for link detection."""
        self._available_macros = list(macros)

    def set_macro(self, macro: dict[str, object] | None) -> None:
        """Store the selected macro without rendering immediately."""
        self._current_macro = macro

    def open(self) -> None:
        """Render the current macro explanation and open the dialog."""
        self.show_macro(self._current_macro)
        self._dialog.open()

    def show_macro(self, macro: dict[str, object] | None) -> None:
        """Rebuild explanation UI for the selected macro."""
        self._current_macro = macro
        payload = explain_macro_script(macro, self._available_macros)
        self._summary_label.set_text(str(payload.get("summary", "")))
        self._render_references(_as_dict_list(payload.get("references", [])))
        self._render_lines(_as_dict_list(payload.get("lines", [])))

    def _render_references(self, references: list[dict[str, object]]) -> None:
        """Render top-level referenced macro links."""
        self._references_row.clear()
        self._references_empty.set_visibility(not references)
        if not references:
            return

        with self._references_row:
            for reference in references:
                label = (
                    str(reference.get("display_name") or reference.get("macro_name", "")).strip()
                    or t("Unnamed macro")
                )
                ui.button(
                    label,
                    on_click=lambda ref=reference: self._open_reference(ref),
                ).props("flat dense no-caps").classes("text-blue-4")

    def _render_lines(self, lines: list[dict[str, object]]) -> None:
        """Render per-line explanation list."""
        self._lines_column.clear()
        if not lines:
            with self._lines_column:
                ui.label(t("No executable script lines are available for this macro.")).classes("text-sm text-grey-5")
            return

        with self._lines_column:
            for line in lines:
                with ui.card().classes("w-full bg-grey-9/60"):
                    with ui.row().classes("w-full items-start gap-3 no-wrap"):
                        ui.label(f"L{line.get('line_number', '?')}").classes(
                            "text-xs font-mono text-grey-5 shrink-0 mt-0.5"
                        )
                        with ui.column().classes("gap-1 flex-1"):
                            ui.label(str(line.get("summary", ""))).classes("text-sm font-medium")
                            ui.label(str(line.get("details", ""))).classes("text-sm text-grey-4")
                            ui.label(str(line.get("text", ""))).classes(
                                "text-xs font-mono text-blue-2 whitespace-pre-wrap"
                            )
                            references = _as_dict_list(line.get("references", []))
                            if references:
                                with ui.row().classes("gap-2 items-center mt-1"):
                                    ui.label(t("Open referenced macro:")).classes("text-xs text-grey-5")
                                    for reference in references:
                                        ui.button(
                                            str(reference.get("display_name") or reference.get("macro_name", "macro")),
                                            on_click=lambda ref=reference: self._open_reference(ref),
                                        ).props("flat dense no-caps").classes("text-yellow-5")

    def _open_reference(self, reference: dict[str, object]) -> None:
        """Jump to a referenced macro and close explanation overlays."""
        if self._open_macro_handler is None:
            return
        self._selected_reference = reference
        self._reference_dialog.close()
        self._dialog.close()
        self._open_macro_handler(
            str(reference.get("file_path", "")),
            str(reference.get("macro_name", "")),
        )

    def _open_reference_dialog(self, reference: dict[str, object]) -> None:
        """Open popup with details for one referenced macro."""
        self._selected_reference = reference
        macro_name = str(reference.get("display_name") or reference.get("macro_name", "")).strip() or t("Referenced macro")
        file_path = str(reference.get("file_path", "")).strip() or t("unknown file")
        is_active = bool(reference.get("is_active", False))
        is_deleted = bool(reference.get("is_deleted", False))

        self._reference_title.set_text(macro_name)
        self._reference_subtitle.set_text(file_path)
        if is_deleted:
            status_text = t("This reference points to a deleted stored macro version.")
        elif is_active:
            status_text = t("This is the active definition.")
        else:
            status_text = t("This definition exists in the vault, but it is currently inactive or overridden.")
        self._reference_status.set_text(status_text)
        self._reference_note.set_text(
            t("Open macro jumps the main viewer to that macro.")
        )
        self._reference_open_button.set_enabled(self._open_macro_handler is not None)
        self._reference_dialog.open()

    def _open_selected_reference(self) -> None:
        """Jump from the popup to the referenced macro."""
        if self._selected_reference is None or self._open_macro_handler is None:
            return
        self._open_reference(self._selected_reference)