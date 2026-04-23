#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reusable explanation panel for Klipper macro scripts."""

from __future__ import annotations

from typing import Callable

from nicegui import ui

from klipper_macro_explainer import explain_macro_script, load_command_pack
from klipper_type_utils import to_dict_list as _as_dict_list
from klipper_type_utils import to_int as _as_int
from klipper_type_utils import to_str_list as _as_str_list
from klipper_vault_i18n import t


def _as_count_dict(value: object) -> dict[str, int]:
    """Normalize dynamic explain payload entries into dict[str, int]."""
    if not isinstance(value, dict):
        return {}

    counts: dict[str, int] = {}
    for key, raw_value in value.items():
        if not isinstance(key, str):
            continue
        try:
            count = int(raw_value)
        except (TypeError, ValueError):
            continue
        if count > 0:
            counts[key] = count
    return counts


def _confidence_classes(confidence: str) -> str:
    """Map confidence labels to compact badge styling."""
    if confidence == "high":
        return "text-xs px-2 py-0.5 rounded bg-green-9 text-green-2"
    if confidence == "medium":
        return "text-xs px-2 py-0.5 rounded bg-yellow-9 text-yellow-2"
    return "text-xs px-2 py-0.5 rounded bg-red-9 text-red-2"


def _effect_classes(effect: str) -> str:
    """Map side-effect tags to compact badge styling."""
    if effect in {"disruptive", "persistent_write"}:
        return "text-xs px-2 py-0.5 rounded bg-red-9 text-red-2"
    if effect in {"blocking_wait", "heater_target_change"}:
        return "text-xs px-2 py-0.5 rounded bg-yellow-9 text-yellow-2"
    return "text-xs px-2 py-0.5 rounded bg-grey-8 text-grey-2"


class MacroExplainerView:
    """Render user-facing explanations for the selected macro."""

    def __init__(self) -> None:
        self._current_macro: dict[str, object] | None = None
        self._available_macros: list[dict[str, object]] = []
        self._open_macro_handler: Callable[[str, str], None] | None = None
        self._selected_reference: dict[str, object] | None = None
        self._verbosity = "detailed"

        with ui.dialog() as self._dialog, ui.card().classes("w-[74rem] max-w-[98vw] h-[86vh] max-h-[94vh] flex flex-col"):
            ui.label(t("Script explanation")).classes("text-md font-semibold")
            with ui.row().classes("w-full items-center gap-2"):
                ui.label(t("Detail level")).classes("text-xs text-grey-5")
                self._verbosity_concise_button = ui.button(
                    t("Concise"), on_click=lambda: self._set_verbosity("concise")
                ).props("dense no-caps flat")
                self._verbosity_detailed_button = ui.button(
                    t("Detailed"), on_click=lambda: self._set_verbosity("detailed")
                ).props("dense no-caps flat")
            self._summary_label = ui.label(t("Select a macro to explain its g-code.")).classes("text-sm text-grey-4")
            self._flow_label = ui.label("").classes("text-xs text-grey-5")
            self._overview_row = ui.row().classes("w-full gap-2 items-center")
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

        self._refresh_verbosity_buttons()

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
        payload = explain_macro_script(
            macro,
            self._available_macros,
            verbosity=self._verbosity,
            command_pack=load_command_pack(),
        )
        self._summary_label.set_text(str(payload.get("summary", "")))
        self._flow_label.set_text(str(payload.get("flow_summary", "")))
        self._render_overview_badges(payload)
        self._render_references(_as_dict_list(payload.get("references", [])))
        self._render_lines(_as_dict_list(payload.get("lines", [])))

    def _set_verbosity(self, verbosity: str) -> None:
        """Update verbosity mode and refresh the currently shown macro."""
        self._verbosity = "concise" if verbosity == "concise" else "detailed"
        self._refresh_verbosity_buttons()
        self.show_macro(self._current_macro)

    def _refresh_verbosity_buttons(self) -> None:
        """Reflect selected verbosity mode in button styles."""
        if self._verbosity == "concise":
            self._verbosity_concise_button.props("dense no-caps color=primary")
            self._verbosity_detailed_button.props("dense no-caps flat")
        else:
            self._verbosity_concise_button.props("dense no-caps flat")
            self._verbosity_detailed_button.props("dense no-caps color=primary")

    def _render_overview_badges(self, payload: dict[str, object]) -> None:
        """Render top-level confidence and risk badges for quick triage."""
        self._overview_row.clear()

        effects = _as_count_dict(payload.get("effects", {}))
        confidence = _as_count_dict(payload.get("confidence", {}))
        risk_line_count = _as_int(payload.get("risk_line_count", 0), default=0)

        with self._overview_row:
            if risk_line_count > 0:
                ui.label(f"{risk_line_count} disruptive line(s)").classes(
                    "text-xs px-2 py-0.5 rounded bg-red-9 text-red-2"
                )
            if effects.get("blocking_wait", 0) > 0:
                ui.label(f"{effects['blocking_wait']} blocking wait(s)").classes(
                    "text-xs px-2 py-0.5 rounded bg-yellow-9 text-yellow-2"
                )
            if effects.get("persistent_write", 0) > 0:
                ui.label(f"{effects['persistent_write']} persistent write(s)").classes(
                    "text-xs px-2 py-0.5 rounded bg-red-9 text-red-2"
                )
            if confidence.get("low", 0) > 0:
                ui.label(f"{confidence['low']} low-confidence line(s)").classes(
                    "text-xs px-2 py-0.5 rounded bg-orange-9 text-orange-2"
                )

            if not any(
                (
                    risk_line_count > 0,
                    effects.get("blocking_wait", 0) > 0,
                    effects.get("persistent_write", 0) > 0,
                    confidence.get("low", 0) > 0,
                )
            ):
                ui.label(t("No elevated effects detected.")).classes("text-xs text-grey-5")

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
                            with ui.row().classes("gap-2 items-center mt-0.5"):
                                confidence = str(line.get("confidence", "")).strip().lower() or "unknown"
                                ui.label(f"confidence: {confidence}").classes(_confidence_classes(confidence))
                                for effect in _as_str_list(line.get("effects", [])):
                                    ui.label(effect.replace("_", " ")).classes(_effect_classes(effect))
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