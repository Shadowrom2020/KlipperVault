#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Timer registration helpers for the NiceGUI frontend."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine

from nicegui import ui


def register_periodic_updates(
    *,
    flush_search: Callable[[], None],
    check_online_updates_on_startup: Callable[[], Coroutine[object, object, None]],
    refresh_create_pr_progress_ui: Callable[[], None],
    refresh_online_update_progress_ui: Callable[[], None],
    check_config_changes: Callable[[], None],
    refresh_off_printer_profile_state: Callable[[], None],
    refresh_printer_card_statuses: Callable[[], None],
) -> None:
    """Register all recurring UI timers in one place."""
    ui.timer(0.25, flush_search)
    ui.timer(0.5, lambda: asyncio.create_task(check_online_updates_on_startup()), once=True)
    ui.timer(0.5, refresh_create_pr_progress_ui)
    ui.timer(0.5, refresh_online_update_progress_ui)
    ui.timer(2.0, check_config_changes)
    ui.timer(5.0, refresh_off_printer_profile_state)
    ui.timer(7.0, refresh_printer_card_statuses)
