import json
import textwrap
from pathlib import Path

from klipper_macro_indexer import (
    get_cfg_load_order,
    load_duplicate_macro_groups,
    load_macro_list,
    macro_row_to_section_text,
    parse_macros_from_cfg,
    remove_inactive_macro_version,
    restore_macro_version,
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


def test_parse_and_render_macro_preserves_rename_existing_line(tmp_path: Path) -> None:
    cfg_path = tmp_path / "printer.cfg"
    _write(
        cfg_path,
        """
        [gcode_macro PAUSE]
        rename_existing: BASE_PAUSE
        gcode:
          RESPOND MSG="custom pause"
        """,
    )

    records = parse_macros_from_cfg(cfg_path, tmp_path)

    assert len(records) == 1
    record = records[0]
    assert record.rename_existing == "BASE_PAUSE"

    rendered = macro_row_to_section_text(
        {
            "section_type": record.section_type,
            "macro_name": record.macro_name,
            "description": record.description,
            "rename_existing": record.rename_existing,
            "gcode": record.gcode,
            "variables_json": record.variables_json,
        }
    )

    assert "rename_existing: BASE_PAUSE" in rendered


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


def test_restore_macro_version_normalizes_blank_lines_around_macro(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    db_path = tmp_path / "db" / "macros.db"
    cfg_path = config_dir / "printer.cfg"

    _write(
        cfg_path,
        """
        [printer]
        kinematics: cartesian
        [gcode_macro HELLO]
        gcode:
          RESPOND MSG="v1"
        [display_status]
        """,
    )
    run_indexing(config_dir, db_path)

    _write(
        cfg_path,
        """
        [printer]
        kinematics: cartesian
        [gcode_macro HELLO]
        gcode:
          RESPOND MSG="v2"
        [display_status]
        """,
    )
    run_indexing(config_dir, db_path)

    restore_macro_version(db_path, config_dir, "printer.cfg", "HELLO", 1)

    assert cfg_path.read_text(encoding="utf-8") == (
        "[printer]\n"
        "kinematics: cartesian\n"
        "\n"
        "[gcode_macro HELLO]\n"
        "gcode:\n"
        "  RESPOND MSG=\"v1\"\n"
        "\n"
        "[display_status]\n"
    )


def test_remove_inactive_macro_version_removes_selected_inactive_row(tmp_path: Path) -> None:
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

    run_indexing(config_dir, db_path)

    base_row = next(row for row in load_macro_list(db_path) if row["file_path"] == "base.cfg")

    result = remove_inactive_macro_version(db_path, "base.cfg", "HELLO", int(base_row["version"]))
    macros = load_macro_list(db_path)

    assert result == {"removed": 1, "reason": "removed"}
    assert len(macros) == 1
    assert macros[0]["file_path"] == "override.cfg"


def test_remove_inactive_macro_version_rejects_active_row(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    db_path = tmp_path / "db" / "macros.db"
    _write(
        config_dir / "printer.cfg",
        """
        [gcode_macro HELLO]
        gcode:
          RESPOND MSG="only"
        """,
    )

    run_indexing(config_dir, db_path)

    active_row = load_macro_list(db_path)[0]

    result = remove_inactive_macro_version(db_path, "printer.cfg", "HELLO", int(active_row["version"]))

    assert result == {"removed": 0, "reason": "not_inactive"}
