#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Integration tests: end-to-end backup → restore and export → import cycles."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import cast

import pytest

from klipper_macro_backup import create_macro_backup, list_macro_backups, load_backup_items, restore_macro_backup
from klipper_macro_indexer import (
    export_macro_share_payload,
    import_macro_share_payload,
    load_macro_list,
    run_indexing,
)


def _cfg(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


# ─── Backup → Restore cycle ───────────────────────────────────────────────────

def test_full_backup_modify_restore_cycle(tmp_path: Path) -> None:
    """Index macros, create backup, mutate files, restore, verify original state."""
    config_dir = tmp_path / "config"
    db_path = tmp_path / "vault.db"

    original = "[gcode_macro HOME_ALL]\ngcode:\n  G28\n"
    _cfg(config_dir / "printer.cfg", original)

    run_indexing(config_dir, db_path)
    macros_before = load_macro_list(db_path)
    assert any(m["macro_name"] == "HOME_ALL" for m in macros_before)

    backup = create_macro_backup(db_path, "pre-change", config_dir=config_dir, now_ts=1000)
    backup_id = cast(int, backup["backup_id"])
    assert backup["macro_count"] == 1

    # Mutate file
    _cfg(config_dir / "printer.cfg", "[gcode_macro HOME_ALL]\ngcode:\n  G28 XYZ\n")
    _cfg(config_dir / "extras.cfg", "[gcode_macro EXTRA_MACRO]\ngcode:\n  RESPOND MSG=\"extra\"\n")
    run_indexing(config_dir, db_path)
    macros_after_mutation = load_macro_list(db_path)
    assert any(m["macro_name"] == "EXTRA_MACRO" for m in macros_after_mutation)

    # Restore backup
    restored = restore_macro_backup(db_path, backup_id, config_dir=config_dir, now_ts=2000)
    assert restored["backup_name"] == "pre-change"
    assert restored["restored_at"] == 2000
    assert restored["restored_cfg_files"] == 1
    assert restored["removed_cfg_files"] == 1

    # File should be back to original
    assert (config_dir / "printer.cfg").read_text(encoding="utf-8") == original
    assert not (config_dir / "extras.cfg").exists()


def test_backup_preserves_multiple_cfg_files(tmp_path: Path) -> None:
    """Backup captures macros across multiple cfg files and restores all."""
    config_dir = tmp_path / "config"
    db_path = tmp_path / "vault.db"

    _cfg(config_dir / "printer.cfg", "[gcode_macro START_PRINT]\ngcode:\n  G28\n")
    _cfg(config_dir / "macros.cfg", "[gcode_macro PAUSE]\ngcode:\n  PAUSE_BASE\n")

    run_indexing(config_dir, db_path)
    backup = create_macro_backup(db_path, "multi-file", config_dir=config_dir, now_ts=100)
    backup_id = cast(int, backup["backup_id"])

    assert backup["macro_count"] == 2
    assert backup["cfg_file_count"] == 2

    items = load_backup_items(db_path, backup_id)
    names = {item["macro_name"] for item in items}
    assert "START_PRINT" in names
    assert "PAUSE" in names

    # Mutate both files
    _cfg(config_dir / "printer.cfg", "[gcode_macro START_PRINT]\ngcode:\n  G28 XYZ\n")
    _cfg(config_dir / "macros.cfg", "[gcode_macro PAUSE]\ngcode:\n  PAUSE_MODIFIED\n")

    restore_macro_backup(db_path, backup_id, config_dir=config_dir, now_ts=200)

    assert "G28\n" in (config_dir / "printer.cfg").read_text(encoding="utf-8")
    assert "PAUSE_BASE" in (config_dir / "macros.cfg").read_text(encoding="utf-8")


def test_multiple_backups_list_and_retrieve(tmp_path: Path) -> None:
    """Multiple backups can be created, listed, and their items retrieved independently."""
    config_dir = tmp_path / "config"
    db_path = tmp_path / "vault.db"

    _cfg(config_dir / "printer.cfg", "[gcode_macro M600]\ngcode:\n  PAUSE\n")
    run_indexing(config_dir, db_path)

    b1 = create_macro_backup(db_path, "backup-v1", config_dir=config_dir, now_ts=10)
    b2 = create_macro_backup(db_path, "backup-v2", config_dir=config_dir, now_ts=20)
    b3 = create_macro_backup(db_path, "backup-v3", config_dir=config_dir, now_ts=30)

    backups = list_macro_backups(db_path)
    names = [b["backup_name"] for b in backups]
    assert "backup-v1" in names
    assert "backup-v2" in names
    assert "backup-v3" in names
    assert len(backups) == 3

    # Each backup has independent items
    for backup in [b1, b2, b3]:
        items = load_backup_items(db_path, cast(int, backup["backup_id"]))
        assert len(items) == 1
        assert items[0]["macro_name"] == "M600"


# ─── Export → Import cycle ────────────────────────────────────────────────────

def test_export_then_import_round_trip(tmp_path: Path) -> None:
    """Export macros to share payload, import into second DB, verify content."""
    src_dir = tmp_path / "src_config"
    src_db = tmp_path / "src.db"
    dst_db = tmp_path / "dst.db"

    _cfg(src_dir / "printer.cfg", (
        "[gcode_macro HEAT_BED]\n"
        "gcode:\n"
        "  M140 S{params.TEMP|default(60)|int}\n"
    ))
    run_indexing(src_dir, src_db)

    # Export
    payload = export_macro_share_payload(
        db_path=src_db,
        identities=[("printer.cfg", "HEAT_BED")],
        source_vendor="Prusa",
        source_model="MK4",
        now_ts=500,
    )
    assert payload["format"] == "klippervault.macro-share.v1"
    assert payload["exported_at"] == 500
    assert payload["source_printer"]["vendor"] == "Prusa"
    assert len(payload["macros"]) == 1
    assert payload["macros"][0]["macro_name"] == "HEAT_BED"

    # Import into a fresh DB
    result = import_macro_share_payload(db_path=dst_db, payload=payload, now_ts=600)
    assert result["imported"] == 1

    imported_macros = load_macro_list(dst_db)
    assert any(m["macro_name"] == "HEAT_BED" for m in imported_macros)


def test_export_multiple_macros_import_all(tmp_path: Path) -> None:
    """Export multiple macros, import them all, verify all arrive."""
    src_dir = tmp_path / "src"
    src_db = tmp_path / "src.db"
    dst_db = tmp_path / "dst.db"

    _cfg(src_dir / "printer.cfg", (
        "[gcode_macro START_PRINT]\ngcode:\n  G28\n\n"
        "[gcode_macro END_PRINT]\ngcode:\n  M104 S0\n\n"
        "[gcode_macro CANCEL_PRINT]\ngcode:\n  CANCEL_PRINT_BASE\n"
    ))
    run_indexing(src_dir, src_db)

    payload = export_macro_share_payload(
        db_path=src_db,
        identities=[
            ("printer.cfg", "START_PRINT"),
            ("printer.cfg", "END_PRINT"),
            ("printer.cfg", "CANCEL_PRINT"),
        ],
        source_vendor="Creality",
        source_model="Ender-3",
    )
    assert len(payload["macros"]) == 3

    result = import_macro_share_payload(db_path=dst_db, payload=payload)
    assert result["imported"] == 3

    imported = load_macro_list(dst_db)
    names = {m["macro_name"] for m in imported}
    assert "START_PRINT" in names
    assert "END_PRINT" in names
    assert "CANCEL_PRINT" in names


def test_import_rejects_oversized_payload(tmp_path: Path) -> None:
    """Import rejects payloads exceeding the 10 MB size limit."""
    db_path = tmp_path / "vault.db"
    large_gcode = "  RESPOND MSG=\"x\"\n" * 500_000  # ~9MB of gcode lines
    payload = {
        "format": "klippervault.macro-share.v1",
        "exported_at": 1000,
        "source_printer": {"vendor": "Test", "model": "Big"},
        "macros": [
            {
                "macro_name": "HUGE_MACRO",
                "source_file_path": "printer.cfg",
                "section_text": f"[gcode_macro HUGE_MACRO]\ngcode:\n{large_gcode}",
            }
        ],
    }
    with pytest.raises(ValueError, match="too large"):
        import_macro_share_payload(db_path=db_path, payload=payload)


def test_import_rejects_invalid_format(tmp_path: Path) -> None:
    """Import rejects payloads with unrecognized format identifier."""
    db_path = tmp_path / "vault.db"
    payload = {
        "format": "unknown-format-v99",
        "macros": [{"macro_name": "TEST", "section_text": "[gcode_macro TEST]\ngcode:\n  G28\n"}],
    }
    with pytest.raises(ValueError, match="unsupported"):
        import_macro_share_payload(db_path=db_path, payload=payload)


def test_export_import_preserves_macro_content(tmp_path: Path) -> None:
    """Exported macro gcode content survives the export→import round trip intact."""
    src_dir = tmp_path / "src"
    src_db = tmp_path / "src.db"
    dst_db = tmp_path / "dst.db"

    gcode_body = "  G28\n  G29\n  G1 Z5 F3000\n  M109 S{params.TEMP|default(200)|int}\n"
    _cfg(src_dir / "printer.cfg", f"[gcode_macro COMPLEX_START]\ngcode:\n{gcode_body}")
    run_indexing(src_dir, src_db)

    payload = export_macro_share_payload(
        db_path=src_db,
        identities=[("printer.cfg", "COMPLEX_START")],
        source_vendor="Test",
        source_model="Printer",
    )
    import_macro_share_payload(db_path=dst_db, payload=payload)

    imported = load_macro_list(dst_db)
    macro = next(m for m in imported if m["macro_name"] == "COMPLEX_START")
    assert "G28" in str(macro.get("gcode", ""))
    assert "G29" in str(macro.get("gcode", ""))
    assert "TEMP" in str(macro.get("gcode", ""))


# ─── Combined backup + export + import cycle ──────────────────────────────────

def test_backup_export_import_combined_cycle(tmp_path: Path) -> None:
    """Full cycle: index → backup → export → import to second DB → restore from backup."""
    config_dir = tmp_path / "config"
    primary_db = tmp_path / "primary.db"
    secondary_db = tmp_path / "secondary.db"

    _cfg(config_dir / "printer.cfg", (
        "[gcode_macro PROBE_CALIBRATE]\ngcode:\n  PROBE_CALIBRATE_BASE\n\n"
        "[gcode_macro BED_MESH_CALIBRATE]\ngcode:\n  BED_MESH_CALIBRATE_BASE\n"
    ))
    run_indexing(config_dir, primary_db)

    # Step 1: Create backup
    backup = create_macro_backup(primary_db, "initial", config_dir=config_dir, now_ts=1000)
    backup_id = cast(int, backup["backup_id"])
    assert backup["macro_count"] == 2

    # Step 2: Export both macros
    payload = export_macro_share_payload(
        db_path=primary_db,
        identities=[
            ("printer.cfg", "PROBE_CALIBRATE"),
            ("printer.cfg", "BED_MESH_CALIBRATE"),
        ],
        source_vendor="Voron",
        source_model="2.4",
    )

    # Step 3: Import to secondary DB (sharing with another printer)
    import_result = import_macro_share_payload(db_path=secondary_db, payload=payload)
    assert import_result["imported"] == 2

    secondary_macros = load_macro_list(secondary_db)
    assert any(m["macro_name"] == "PROBE_CALIBRATE" for m in secondary_macros)
    assert any(m["macro_name"] == "BED_MESH_CALIBRATE" for m in secondary_macros)

    # Step 4: Mutate primary config
    _cfg(config_dir / "printer.cfg", "[gcode_macro PROBE_CALIBRATE]\ngcode:\n  MODIFIED\n")

    # Step 5: Restore primary from backup
    restore_macro_backup(primary_db, backup_id, config_dir=config_dir, now_ts=2000)
    assert "PROBE_CALIBRATE_BASE" in (config_dir / "printer.cfg").read_text(encoding="utf-8")
