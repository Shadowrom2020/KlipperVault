#!/usr/bin/env python3
# Copyright (C) 2026 Juergen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Version comparison wrapper for KlipperVault macros."""

from __future__ import annotations

from klipper_macro_compare_core import MacroCompareDialog, format_ts
from klipper_vault_i18n import t


class MacroCompareView:
    """Dialog wrapper that compares stored versions of one macro."""

    def __init__(self) -> None:
        self._dialog = MacroCompareDialog()

    def set_macro(self, macro: dict | None, versions: list[dict]) -> None:
        """Load one macro and its version set into the compare dialog state."""
        macro_name = str(macro.get("macro_name", "")) if macro else ""

        def version_label(version_key: int, entry: dict) -> str:
            custom = str(entry.get("compare_label") or "").strip()
            if custom:
                return custom
            return f"v{version_key}  {format_ts(int(entry.get('indexed_at', 0)))}"

        default_left = int(versions[0]["version"]) if versions else None
        default_right = int(versions[1]["version"]) if len(versions) >= 2 else default_left

        subtitle = (
            t("Compare stored versions for {macro_name}.", macro_name=macro_name)
            if versions
            else t("Choose two versions to compare.")
        )

        self._dialog.set_entries(
            subject_name=macro_name,
            entries=versions,
            title=t("Compare macro versions"),
            subtitle=subtitle,
            label_builder=version_label,
            default_left=default_left,
            default_right=default_right,
        )

    def open(self) -> None:
        """Open the compare dialog."""
        self._dialog.open()
