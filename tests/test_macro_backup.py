import textwrap
from pathlib import Path
from typing import cast

from klipper_macro_backup import create_macro_backup, list_macro_backups, load_backup_items, restore_macro_backup
from klipper_macro_indexer import load_macro_list, run_indexing
from klipper_vault_config_source import LocalConfigSource


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


def test_backup_creation_listing_and_restore_round_trip(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    db_path = tmp_path / "db" / "vault.db"
    original_printer_cfg = "[gcode_macro PRINT_TEST]\ngcode:\n  RESPOND MSG=\"backup\"\n"
    _write(config_dir / "printer.cfg", original_printer_cfg)

    run_indexing(config_dir, db_path)

    backup = create_macro_backup(db_path, "nightly", config_dir=config_dir, now_ts=111)
    backup_id = cast(int, backup["backup_id"])
    backups = list_macro_backups(db_path)
    items = load_backup_items(db_path, backup_id)

    assert backup["backup_name"] == "nightly"
    assert backup["created_at"] == 111
    assert backup["macro_count"] == 1
    assert backup["cfg_file_count"] == 1
    assert backups[0]["backup_id"] == backup_id
    assert backups[0]["macro_count"] == 1
    assert items[0]["macro_name"] == "PRINT_TEST"
    assert items[0]["file_path"] == "printer.cfg"

    _write(
        config_dir / "printer.cfg",
        """
        [gcode_macro PRINT_TEST]
        gcode:
          RESPOND MSG="mutated"
        """,
    )
    _write(config_dir / "extra.cfg", "[printer]\nkinematics: corexy\n")

    restored = restore_macro_backup(db_path, backup_id, config_dir=config_dir, now_ts=222)
    run_indexing(config_dir, db_path)
    restored_macros = load_macro_list(db_path)

    assert restored["backup_name"] == "nightly"
    assert restored["restored_at"] == 222
    assert restored["restored_cfg_files"] == 1
    assert restored["removed_cfg_files"] == 1
    assert (config_dir / "printer.cfg").read_text(encoding="utf-8") == original_printer_cfg
    assert not (config_dir / "extra.cfg").exists()
    assert len(restored_macros) == 1
    assert restored_macros[0]["macro_name"] == "PRINT_TEST"


def test_backup_round_trip_with_config_source(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    db_path = tmp_path / "db" / "vault.db"
    original_printer_cfg = "[gcode_macro PRINT_TEST]\ngcode:\n  RESPOND MSG=\"backup\"\n"
    _write(config_dir / "printer.cfg", original_printer_cfg)

    run_indexing(config_dir, db_path)

    source = LocalConfigSource(root_dir=config_dir)
    backup = create_macro_backup(db_path, "source-backup", config_source=source, now_ts=333)

    _write(
        config_dir / "printer.cfg",
        """
        [gcode_macro PRINT_TEST]
        gcode:
            RESPOND MSG="mutated"
        """,
    )
    _write(config_dir / "extra.cfg", "[printer]\nkinematics: corexy\n")

    restored = restore_macro_backup(db_path, cast(int, backup["backup_id"]), config_source=source, now_ts=444)

    assert restored["backup_name"] == "source-backup"
    assert restored["restored_at"] == 444
    assert restored["restored_cfg_files"] == 1
    assert restored["removed_cfg_files"] == 1
    assert (config_dir / "printer.cfg").read_text(encoding="utf-8") == original_printer_cfg
    assert not (config_dir / "extra.cfg").exists()


def test_restore_removes_macros_cfg_when_not_present_in_backup(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    db_path = tmp_path / "db" / "vault.db"
    _write(
        config_dir / "printer.cfg",
        """
        [gcode_macro START_PRINT]
        gcode:
            G28
        """,
    )

    run_indexing(config_dir, db_path)
    backup = create_macro_backup(db_path, "before-migration", config_dir=config_dir, now_ts=123)
    backup_id = cast(int, backup["backup_id"])
    assert backup["cfg_file_count"] == 1

    _write(
        config_dir / "macros.cfg",
        """
        [gcode_macro START_PRINT]
        gcode:
            G28
        """,
    )
    _write(
        config_dir / "printer.cfg",
        """
        [include macros.cfg]
        """,
    )

    restored = restore_macro_backup(db_path, backup_id, config_dir=config_dir, now_ts=456)

    assert restored["restored_cfg_files"] == 1
    assert restored["removed_cfg_files"] == 1
    assert "macros.cfg" in cast(list[str], restored["removed_cfg_paths"])
    assert not (config_dir / "macros.cfg").exists()
    assert "[gcode_macro START_PRINT]" in (config_dir / "printer.cfg").read_text(encoding="utf-8")

def test_restore_does_not_overwrite_printer_cfg_when_backup_has_no_printer_macros(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    db_path = tmp_path / "db" / "vault.db"
    _write(
        config_dir / "printer.cfg",
        """
        [include macros.cfg]

        [printer]
        kinematics: corexy
        """,
    )
    _write(
        config_dir / "macros.cfg",
        """
        [gcode_macro START_PRINT]
        gcode:
            G28
        """,
    )

    backup = create_macro_backup(db_path, "no-printer-macros", config_dir=config_dir, now_ts=11)
    backup_id = cast(int, backup["backup_id"])

    # Mutate local files after backup so restore needs to apply snapshot.
    _write(
        config_dir / "printer.cfg",
        """
        [include macros.cfg]

        [printer]
        kinematics: cartesian
        """,
    )
    _write(
        config_dir / "macros.cfg",
        """
        [gcode_macro START_PRINT]
        gcode:
            M117 restored
        """,
    )
    _write(config_dir / "extra.cfg", "[heater_bed]\n")

    restored = restore_macro_backup(db_path, backup_id, config_dir=config_dir, now_ts=22)

    # printer.cfg stays untouched because backup printer.cfg has no macros.
    assert restored["printer_cfg_overwritten"] is False
    assert "kinematics: cartesian" in (config_dir / "printer.cfg").read_text(encoding="utf-8")
    assert "M117 restored" not in (config_dir / "macros.cfg").read_text(encoding="utf-8")
    assert not (config_dir / "extra.cfg").exists()
