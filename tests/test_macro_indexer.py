import json
import textwrap
from pathlib import Path

from klipper_macro_indexer import (
    get_cfg_load_order,
    load_duplicate_macro_groups,
    load_macro_list,
    parse_macros_from_cfg,
    run_indexing,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


def test_parse_macros_trims_trailing_comments_and_preserves_indented_brackets(tmp_path: Path) -> None:
    cfg_path = tmp_path / "printer.cfg"
    _write(
        cfg_path,
        """
        [gcode_macro PRINT_START]
        description: Startup macro
        variable_speed: 120
        gcode:
          G28
          [not a real header]
          RESPOND MSG="ready"
          # trailing comment trimmed
          ; trailing semicolon trimmed

        [printer]
        kinematics: cartesian
        """,
    )

    records = parse_macros_from_cfg(cfg_path, tmp_path)

    assert len(records) == 1
    record = records[0]
    assert record.file_path == "printer.cfg"
    assert record.section_type == "gcode_macro"
    assert record.macro_name == "PRINT_START"
    assert record.line_number == 1
    assert record.description == "Startup macro"
    assert json.loads(record.variables_json) == {"speed": "120"}
    assert record.gcode == '  G28\n  [not a real header]\n  RESPOND MSG="ready"'


def test_get_cfg_load_order_follows_include_order_and_appends_unreferenced_files(tmp_path: Path) -> None:
    _write(
        tmp_path / "printer.cfg",
        """
        [include extras/b.cfg]
        [include extras/a.cfg]
        """,
    )
    _write(tmp_path / "extras" / "a.cfg", "[printer]\n")
    _write(tmp_path / "extras" / "b.cfg", "[printer]\n")
    _write(tmp_path / "orphan.cfg", "[printer]\n")

    order = [str(path.relative_to(tmp_path)) for path in get_cfg_load_order(tmp_path)]

    assert order == ["printer.cfg", "extras/b.cfg", "extras/a.cfg", "orphan.cfg"]


def test_run_indexing_marks_only_last_duplicate_macro_active(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    db_path = tmp_path / "db" / "macros.db"
    _write(
        config_dir / "printer.cfg",
        """
        [include base.cfg]
        [include override.cfg]
        """,
    )
    _write(
        config_dir / "base.cfg",
        """
        [gcode_macro HELLO]
        gcode:
          RESPOND MSG="base"
        """,
    )
    _write(
        config_dir / "override.cfg",
        """
        [gcode_macro HELLO]
        gcode:
          RESPOND MSG="override"
        """,
    )

    result = run_indexing(config_dir, db_path)
    macros = load_macro_list(db_path)
    rows_by_path = {row["file_path"]: row for row in macros}

    assert result["cfg_files_scanned"] == 3
    assert set(rows_by_path) == {"base.cfg", "override.cfg"}
    assert rows_by_path["base.cfg"]["is_active"] is False
    assert rows_by_path["override.cfg"]["is_active"] is True
    assert rows_by_path["override.cfg"]["runtime_macro_name"] == "HELLO"


def test_renamed_runtime_alias_not_counted_as_duplicate(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    db_path = tmp_path / "db" / "macros.db"
    _write(
        config_dir / "printer.cfg",
        """
        [include base.cfg]
        [include override.cfg]
        """,
    )
    _write(
        config_dir / "base.cfg",
        """
        [gcode_macro HELLO]
        gcode:
          RESPOND MSG="base"
        """,
    )
    _write(
        config_dir / "override.cfg",
        """
        [gcode_macro HELLO]
        rename_existing: OLD_HELLO
        gcode:
          RESPOND MSG="override"
        """,
    )

    run_indexing(config_dir, db_path)

    groups = load_duplicate_macro_groups(db_path)
    assert groups == []
