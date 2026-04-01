#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Filesystem watcher helpers for Klipper cfg trees.

This module provides a lightweight polling-based watcher that tracks
creation, deletion, and modification of *.cfg files under a config directory.
"""

from __future__ import annotations

from pathlib import Path


class ConfigWatcher:
    """Track cfg tree snapshots and detect changes between polls."""

    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self._last_snapshot: dict[str, tuple[int, int]] = {}

    def _build_snapshot(self) -> dict[str, tuple[int, int]]:
        """Build a stable map of cfg relative paths to (mtime_ns, size)."""
        snapshot: dict[str, tuple[int, int]] = {}
        if not self.config_dir.exists():
            return snapshot

        for cfg_path in self.config_dir.rglob("*.cfg"):
            if not cfg_path.is_file():
                continue
            try:
                stat = cfg_path.stat()
                rel = str(cfg_path.relative_to(self.config_dir))
                snapshot[rel] = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                continue
        return snapshot

    def reset(self) -> None:
        """Refresh baseline snapshot to current filesystem state."""
        self._last_snapshot = self._build_snapshot()

    def poll_changed(self) -> bool:
        """Return True when cfg files were added/removed/changed since last poll."""
        current_snapshot = self._build_snapshot()

        if not self._last_snapshot:
            self._last_snapshot = current_snapshot
            return False

        changed = False
        if current_snapshot.keys() != self._last_snapshot.keys():
            changed = True
        else:
            for rel_path, meta in current_snapshot.items():
                if self._last_snapshot.get(rel_path) != meta:
                    changed = True
                    break

        if changed:
            self._last_snapshot = current_snapshot
        return changed
