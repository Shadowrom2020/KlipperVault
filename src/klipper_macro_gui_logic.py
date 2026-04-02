#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure UI-state helpers for the KlipperVault GUI."""

from __future__ import annotations

from collections import Counter


def _display_macro_name(macro: dict[str, object]) -> str:
    """Return runtime-visible macro name for UI/filtering."""
    return str(macro.get("display_name") or macro.get("runtime_macro_name") or macro.get("macro_name", ""))


def macro_key(macro: dict[str, object]) -> str:
    """Build stable in-memory key used for list selection state."""
    return f"{macro['file_path']}::{macro['macro_name']}"


def duplicate_names_for_macros(macros: list[dict[str, object]]) -> set[str]:
    """Collect case-insensitive names that appear in multiple active rows."""
    name_counts = Counter(
        _display_macro_name(m).lower()
        for m in macros
        if not bool(m.get("is_deleted", False))
    )
    return {name for name, count in name_counts.items() if count > 1}


def filter_macros(
    macros: list[dict[str, object]],
    search_query: str,
    show_duplicates_only: bool,
    active_filter: str,
    duplicate_names: set[str],
) -> list[dict[str, object]]:
    """Apply search/duplicate/active filters to macro list."""
    query = search_query.strip().lower()
    return [
        macro
        for macro in macros
        if (
            (not query)
            or query in _display_macro_name(macro).lower()
            or query in str(macro.get("macro_name", "")).lower()
            or query in str(macro.get("file_path", "")).lower()
        )
        and (
            (not show_duplicates_only)
            or _display_macro_name(macro).lower() in duplicate_names
        )
        and (
            active_filter == "all"
            or (active_filter == "active" and bool(macro.get("is_active", False)))
            or (active_filter == "inactive" and not bool(macro.get("is_active", False)))
        )
    ]


def selected_or_first_macro(
    visible_macros: list[dict[str, object]],
    selected_key: str | None,
) -> dict[str, object] | None:
    """Return previously selected macro if still visible, otherwise first row."""
    if not visible_macros:
        return None
    if selected_key:
        for macro in visible_macros:
            if macro_key(macro) == selected_key:
                return macro
    return visible_macros[0]


def find_active_override(
    selected_macro: dict[str, object],
    macros: list[dict[str, object]],
) -> dict[str, object] | None:
    """Find active counterpart for an inactive selected macro."""
    if bool(selected_macro.get("is_active", False)):
        return None

    selected_name = _display_macro_name(selected_macro).lower()
    selected_path = str(selected_macro.get("file_path", ""))
    for macro in macros:
        if (
            bool(macro.get("is_active", False))
            and _display_macro_name(macro).lower() == selected_name
            and str(macro.get("file_path", "")) != selected_path
        ):
            return macro
    return None


def duplicate_count_from_stats(stats: dict[str, object]) -> int:
    """Calculate duplicate count using existing dashboard aggregates."""
    return max(int(stats["total_macros"]) - int(stats["distinct_macro_names"]), 0)


_SORT_ORDERS = ("alpha_asc", "alpha_desc", "load_order")
_SORT_LABELS = {
    "alpha_asc": "A → Z",
    "alpha_desc": "Z → A",
    "load_order": "Load order",
}


def next_sort_order(current: str) -> str:
    """Cycle to the next sort order."""
    idx = _SORT_ORDERS.index(current) if current in _SORT_ORDERS else 0
    return _SORT_ORDERS[(idx + 1) % len(_SORT_ORDERS)]


def sort_label(order: str) -> str:
    """Human-readable label for a sort order."""
    return _SORT_LABELS.get(order, order)


def sort_macros(macros: list[dict[str, object]], order: str) -> list[dict[str, object]]:
    """Return a sorted copy of *macros* according to *order*.

    ``alpha_asc``  – A→Z by display name (case-insensitive).
    ``alpha_desc`` – Z→A by display name (case-insensitive).
    ``load_order`` – file path then line number (Klipper's parse order).
    """
    if order == "alpha_asc":
        return sorted(macros, key=lambda m: _display_macro_name(m).lower())
    if order == "alpha_desc":
        return sorted(macros, key=lambda m: _display_macro_name(m).lower(), reverse=True)
    if order == "load_order":
        return sorted(macros, key=lambda m: (str(m.get("file_path", "")), int(m.get("line_number", 0))))
    return list(macros)
