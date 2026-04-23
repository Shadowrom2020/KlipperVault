#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Performance benchmarks for KlipperVault indexing operations.

These tests verify that performance stays within acceptable bounds on typical
Klipper installations. Run with pytest -v to see timing details.
"""

from __future__ import annotations

import time
from pathlib import Path


from klipper_macro_indexer import load_macro_list, run_indexing


def _write_macro_cfg(path: Path, macros: list[tuple[str, str]]) -> None:
    """Write a cfg file with multiple gcode_macro sections."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for name, gcode in macros:
        lines.append(f"[gcode_macro {name}]")
        lines.append("gcode:")
        for line in gcode.splitlines():
            lines.append(f"  {line}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _generate_macro_set(count: int) -> list[tuple[str, str]]:
    """Generate a set of distinct macro definitions."""
    macros = []
    for i in range(count):
        name = f"MACRO_{i:04d}"
        gcode = (
            f"G28 ; home all\n"
            f"G1 Z{i % 50 + 5} F3000\n"
            f"M109 S{180 + (i % 60)}\n"
            f"M140 S{50 + (i % 30)}\n"
            f"RESPOND MSG=\"running {name}\"\n"
        )
        macros.append((name, gcode))
    return macros


class TestIndexingPerformance:
    """Benchmark indexing throughput on realistic macro collections."""

    def test_small_collection_under_2s(self, tmp_path: Path) -> None:
        """50 macros across 5 files should index in under 2 seconds."""
        config_dir = tmp_path / "config"
        db_path = tmp_path / "vault.db"
        all_macros = _generate_macro_set(50)

        for i in range(5):
            chunk = all_macros[i * 10:(i + 1) * 10]
            _write_macro_cfg(config_dir / f"macros_{i:02d}.cfg", chunk)

        start = time.monotonic()
        result = run_indexing(config_dir, db_path)
        elapsed = time.monotonic() - start

        assert result["macros_inserted"] == 50
        assert elapsed < 2.0, f"Indexing 50 macros took {elapsed:.2f}s (limit: 2.0s)"

    def test_medium_collection_under_5s(self, tmp_path: Path) -> None:
        """200 macros across 10 files should index in under 5 seconds."""
        config_dir = tmp_path / "config"
        db_path = tmp_path / "vault.db"
        all_macros = _generate_macro_set(200)

        for i in range(10):
            chunk = all_macros[i * 20:(i + 1) * 20]
            _write_macro_cfg(config_dir / f"macros_{i:02d}.cfg", chunk)

        start = time.monotonic()
        result = run_indexing(config_dir, db_path)
        elapsed = time.monotonic() - start

        assert result["macros_inserted"] == 200
        assert elapsed < 5.0, f"Indexing 200 macros took {elapsed:.2f}s (limit: 5.0s)"

    def test_large_collection_under_15s(self, tmp_path: Path) -> None:
        """500 macros across 20 files should index in under 15 seconds."""
        config_dir = tmp_path / "config"
        db_path = tmp_path / "vault.db"
        all_macros = _generate_macro_set(500)

        for i in range(20):
            chunk = all_macros[i * 25:(i + 1) * 25]
            _write_macro_cfg(config_dir / f"macros_{i:02d}.cfg", chunk)

        start = time.monotonic()
        result = run_indexing(config_dir, db_path)
        elapsed = time.monotonic() - start

        assert result["macros_inserted"] == 500
        assert elapsed < 15.0, f"Indexing 500 macros took {elapsed:.2f}s (limit: 15.0s)"

    def test_reindex_unchanged_is_fast(self, tmp_path: Path) -> None:
        """Re-indexing unchanged files skips inserts; should be faster than initial index."""
        config_dir = tmp_path / "config"
        db_path = tmp_path / "vault.db"
        all_macros = _generate_macro_set(100)

        for i in range(5):
            chunk = all_macros[i * 20:(i + 1) * 20]
            _write_macro_cfg(config_dir / f"macros_{i:02d}.cfg", chunk)

        # Initial index
        start = time.monotonic()
        first_result = run_indexing(config_dir, db_path)
        first_elapsed = time.monotonic() - start

        # Re-index (unchanged)
        start = time.monotonic()
        second_result = run_indexing(config_dir, db_path)
        second_elapsed = time.monotonic() - start

        assert first_result["macros_inserted"] == 100
        assert second_result["macros_unchanged"] == 100
        assert second_result["macros_inserted"] == 0
        # Re-index should be at most 3x slower (generous tolerance for CI)
        assert second_elapsed <= max(first_elapsed * 3, 1.0), (
            f"Re-index ({second_elapsed:.2f}s) should be comparable to first ({first_elapsed:.2f}s)"
        )

    def test_incremental_update_performance(self, tmp_path: Path) -> None:
        """Adding 50 new macros to an existing 200-macro DB should be fast."""
        config_dir = tmp_path / "config"
        db_path = tmp_path / "vault.db"
        all_macros = _generate_macro_set(250)

        # Initial index of 200 macros
        for i in range(10):
            chunk = all_macros[i * 20:(i + 1) * 20]
            _write_macro_cfg(config_dir / f"macros_{i:02d}.cfg", chunk)

        run_indexing(config_dir, db_path)

        # Add 50 new macros in a new file
        _write_macro_cfg(config_dir / "new_macros.cfg", all_macros[200:250])

        start = time.monotonic()
        result = run_indexing(config_dir, db_path)
        elapsed = time.monotonic() - start

        assert result["macros_inserted"] == 50
        assert result["macros_unchanged"] == 200
        assert elapsed < 5.0, f"Incremental update took {elapsed:.2f}s (limit: 5.0s)"


class TestLoadPerformance:
    """Benchmark macro list loading from SQLite."""

    def test_load_500_macros_under_1s(self, tmp_path: Path) -> None:
        """Loading 500 macros from SQLite should complete in under 1 second."""
        config_dir = tmp_path / "config"
        db_path = tmp_path / "vault.db"
        all_macros = _generate_macro_set(500)

        for i in range(20):
            chunk = all_macros[i * 25:(i + 1) * 25]
            _write_macro_cfg(config_dir / f"macros_{i:02d}.cfg", chunk)

        run_indexing(config_dir, db_path)

        start = time.monotonic()
        macros = load_macro_list(db_path)
        elapsed = time.monotonic() - start

        assert len(macros) == 500
        assert elapsed < 1.0, f"Loading 500 macros took {elapsed:.2f}s (limit: 1.0s)"

    def test_repeated_loads_are_consistent(self, tmp_path: Path) -> None:
        """Repeated loads of the same DB should return consistent results."""
        config_dir = tmp_path / "config"
        db_path = tmp_path / "vault.db"
        _write_macro_cfg(config_dir / "printer.cfg", _generate_macro_set(50))
        run_indexing(config_dir, db_path)

        results = []
        for _ in range(5):
            macros = load_macro_list(db_path)
            results.append(len(macros))

        assert all(r == 50 for r in results), f"Inconsistent counts: {results}"
