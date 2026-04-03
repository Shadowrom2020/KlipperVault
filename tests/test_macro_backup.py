import textwrap
from pathlib import Path

from klipper_macro_backup import create_macro_backup, list_macro_backups, load_backup_items, restore_macro_backup
from klipper_macro_indexer import load_macro_list, run_indexing


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
    backups = list_macro_backups(db_path)
    items = load_backup_items(db_path, backup["backup_id"])

    assert backup["backup_name"] == "nightly"
    assert backup["created_at"] == 111
    assert backup["macro_count"] == 1
    assert backup["cfg_file_count"] == 1
    assert backups[0]["backup_id"] == backup["backup_id"]
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

    restored = restore_macro_backup(db_path, backup["backup_id"], config_dir=config_dir, now_ts=222)
    restored_macros = load_macro_list(db_path)

    assert restored["backup_name"] == "nightly"
    assert restored["restored_at"] == 222
    assert restored["restored_cfg_files"] == 1
    assert restored["removed_cfg_files"] == 1
    assert (config_dir / "printer.cfg").read_text(encoding="utf-8") == original_printer_cfg
    assert not (config_dir / "extra.cfg").exists()
    assert len(restored_macros) == 1
    assert restored_macros[0]["macro_name"] == "PRINT_TEST"
