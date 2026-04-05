#!/usr/bin/env python3
# Copyright (C) 2026 Juergen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reusable side-by-side macro comparison dialog."""

from __future__ import annotations

from datetime import datetime
import json
import uuid
from typing import Callable

from nicegui import ui
from klipper_vault_i18n import t


def format_ts(epoch_ts: int | None) -> str:
    """Format unix timestamp for compare labels."""
    if epoch_ts is None:
        return t("never")
    return datetime.fromtimestamp(epoch_ts).strftime("%Y-%m-%d %H:%M:%S")


def macro_to_text(macro: dict | None) -> str:
    """Render one macro row as INI-like text for code display widgets."""
    if macro is None:
        return ""

    description = str(macro.get("description") or "")
    rename_existing = str(macro.get("rename_existing") or "").strip()
    gcode = str(macro.get("gcode") or "")
    variables_raw = str(macro.get("variables_json") or "{}")
    try:
        variables_obj = json.loads(variables_raw)
        variables_lines = json.dumps(variables_obj, indent=2, sort_keys=True).splitlines()
    except json.JSONDecodeError:
        variables_lines = variables_raw.splitlines() or [variables_raw]

    lines = [f"[gcode_macro {macro.get('macro_name', '')}]"]
    if description:
        lines.append(f"description: {description}")
    if rename_existing:
        lines.append(f"rename_existing: {rename_existing}")
    for i, part in enumerate(variables_lines):
        if i == 0:
            lines.append("variables_json: " + part)
        else:
            lines.append("  " + part)
    if gcode:
        lines.append("gcode:")
        for line in gcode.splitlines():
            lines.append(f"  {line}")
    return "\n".join(lines)


class MacroCompareDialog:
    """Reusable two-pane comparison dialog for macro-like entries."""

    def __init__(self) -> None:
        self._subject_name = ""
        self._entries: dict[int, dict] = {}
        self._label_builder: Callable[[int, dict], str] = lambda k, _: str(k)
        self._text_builder: Callable[[dict], str] = macro_to_text
        self._sync_group = f"kv-compare-{uuid.uuid4().hex}"

        ui.add_body_html(
            """
            <script>
            if (!window.kvInstallComparePaneSync) {
              window.kvBindComparePaneSync = function() {
                document.querySelectorAll('.kv-compare-sync-root').forEach(function(root) {
                  const left = root.querySelector('.kv-compare-left-pane');
                  const right = root.querySelector('.kv-compare-right-pane');
                  if (!left || !right) return;
                  if (left.dataset.kvPaneBound === '1' && right.dataset.kvPaneBound === '1') return;

                  let syncing = false;
                  const sync = function(src, dst) {
                    if (syncing) return;
                    syncing = true;
                    dst.scrollTop = src.scrollTop;
                    dst.scrollLeft = src.scrollLeft;
                    requestAnimationFrame(function() { syncing = false; });
                  };

                  left.addEventListener('scroll', function() { sync(left, right); }, {passive: true});
                  right.addEventListener('scroll', function() { sync(right, left); }, {passive: true});
                  left.dataset.kvPaneBound = '1';
                  right.dataset.kvPaneBound = '1';
                });
              };

              window.kvInstallComparePaneSync = true;
              const observer = new MutationObserver(function() {
                if (window.kvBindComparePaneSync) {
                  window.kvBindComparePaneSync();
                }
              });
              observer.observe(document.body, { childList: true, subtree: true });

              if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', function() {
                  if (window.kvBindComparePaneSync) window.kvBindComparePaneSync();
                });
              } else {
                window.kvBindComparePaneSync();
              }
            }
            </script>
            """
        )

        with ui.dialog() as self._dialog, ui.card().classes(
            f"w-[98vw] h-[94vh] max-w-none flex flex-col kv-compare-sync-root {self._sync_group}"
        ):
            self._title_label = ui.label(t("Compare macro versions")).classes("text-xl font-bold")
            self._subtitle = ui.label(t("Choose two versions to compare.")).classes("text-sm text-grey-5")
            with ui.row().classes("w-full items-end gap-3"):
                self._left_select = (
                    ui.select(options={}, label=t("Left"), on_change=lambda _: self._render_diff())
                    .props("dense outlined")
                    .classes("w-72")
                )
                self._right_select = (
                    ui.select(options={}, label=t("Right"), on_change=lambda _: self._render_diff())
                    .props("dense outlined")
                    .classes("w-72")
                )
                ui.space()
                ui.button(t("Close"), on_click=self._dialog.close).props("flat no-caps")
            self._message = ui.label("").classes("text-sm text-grey-4 mt-2")
            with ui.grid().classes("w-full grid-cols-2 gap-3 flex-1 min-h-0 mt-3"):
                with ui.card().classes("h-full min-h-0 overflow-auto kv-compare-left-pane"):
                    self._left_header = ui.label(t("Left")).classes("text-xs text-grey-4 mb-1")
                    self._left_code = ui.code("", language="ini").classes("w-full")
                with ui.card().classes("h-full min-h-0 overflow-auto kv-compare-right-pane"):
                    self._right_header = ui.label(t("Right")).classes("text-xs text-grey-4 mb-1")
                    self._right_code = ui.code("", language="ini").classes("w-full")

    def _set_code_content(self, element, content: str) -> None:
        """Set content on ui.code element across NiceGUI API variants."""
        if hasattr(element, "set_content"):
            element.set_content(content)
            return
        if hasattr(element, "content"):
            element.content = content
            element.update()
            return
        element.update()

    def _set_codeboxes(self, left_text: str, right_text: str) -> None:
        """Update both left/right code panes."""
        self._set_code_content(self._left_code, left_text)
        self._set_code_content(self._right_code, right_text)

    def _render_diff(self) -> None:
        """Refresh headers, status text, and code panes from selected entries."""
        left_key = self._left_select.value
        right_key = self._right_select.value
        if left_key is None or right_key is None:
            self._message.set_text(t("Choose two entries to compare."))
            self._left_header.set_text(t("Left"))
            self._right_header.set_text(t("Right"))
            self._set_codeboxes("", "")
            return

        left_entry = self._entries.get(int(left_key))
        right_entry = self._entries.get(int(right_key))
        if left_entry is None or right_entry is None:
            self._message.set_text(t("Selected entry data is not available."))
            self._left_header.set_text(t("Left"))
            self._right_header.set_text(t("Right"))
            self._set_codeboxes("", "")
            return

        left_label = self._label_builder(int(left_key), left_entry)
        right_label = self._label_builder(int(right_key), right_entry)
        self._left_header.set_text(left_label)
        self._right_header.set_text(right_label)
        self._set_codeboxes(self._text_builder(left_entry), self._text_builder(right_entry))

        if int(left_key) == int(right_key):
            self._message.set_text(t(
                "Showing identical entries for {subject}: {label} on both sides.",
                subject=self._subject_name,
                label=left_label,
            ))
            return

        self._message.set_text(t(
            "Showing {subject}: left {left} vs right {right}.",
            subject=self._subject_name,
            left=left_label,
            right=right_label,
        ))

    def set_entries(
        self,
        subject_name: str,
        entries: list[dict],
        title: str,
        subtitle: str,
        label_builder: Callable[[int, dict], str],
        text_builder: Callable[[dict], str] | None = None,
        default_left: int | None = None,
        default_right: int | None = None,
    ) -> None:
        """Load arbitrary entries into comparison dialog."""
        self._subject_name = subject_name
        self._entries = {int(entry["version"]): entry for entry in entries}
        self._label_builder = label_builder
        self._text_builder = text_builder or macro_to_text

        self._title_label.set_text(title)
        self._subtitle.set_text(subtitle)

        options = {
            int(entry["version"]): self._label_builder(int(entry["version"]), entry)
            for entry in entries
        }
        self._left_select.options = options
        self._right_select.options = options

        left_choice = default_left
        right_choice = default_right
        if left_choice is None and entries:
            left_choice = int(entries[0]["version"])
        if right_choice is None and len(entries) >= 2:
            right_choice = int(entries[1]["version"])
        elif right_choice is None:
            right_choice = left_choice

        self._left_select.value = left_choice
        self._right_select.value = right_choice
        self._left_select.update()
        self._right_select.update()
        self._render_diff()

    def open(self) -> None:
        """Open the compare dialog."""
        self._dialog.open()
