#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared runtime type normalization helpers."""

from __future__ import annotations


def to_int(value: object, default: int = 0) -> int:
    """Convert dynamic values to int with a safe fallback."""
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


def to_text(value: object) -> str:
    """Normalize dynamic values into stripped text."""
    return str(value or "").strip()


def to_dict_list(value: object) -> list[dict[str, object]]:
    """Normalize dynamic values into a list of dictionary payloads."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def to_str_list(value: object) -> list[str]:
    """Normalize dynamic values into a list of strings."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
