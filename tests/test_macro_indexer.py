import json
import textwrap
from pathlib import Path

from klipper_macro_online_update import import_online_macro_updates
from klipper_macro_indexer import (
    export_macro_share_payload,
    get_cfg_load_order,
    get_cfg_load_order_from_source,
    get_cfg_loading_overview,
    get_cfg_loading_overview_from_source,
    import_macro_share_payload,
    load_duplicate_macro_groups,
    load_macro_list,
    macro_row_to_section_text,
    parse_macros_from_cfg,
    remove_deleted_macro,
    remove_inactive_macro_version,
    restore_macro_version,
    run_indexing,
    run_indexing_from_source,
)
from klipper_vault_config_source import LocalConfigSource
from klipper_vault_db import open_sqlite_connection


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


def test_get_cfg_load_order_includes_dynamicmacros_configs(tmp_path: Path) -> None:
    _write(
        tmp_path / "printer.cfg",
        """
        [dynamicmacros]
        configs: generated/one.cfg, generated/two.cfg
        """,
    )
    _write(tmp_path / "generated" / "one.cfg", "[printer]\n")
    _write(tmp_path / "generated" / "two.cfg", "[printer]\n")
    _write(tmp_path / "orphan.cfg", "[printer]\n")

    order = [str(path.relative_to(tmp_path)) for path in get_cfg_load_order(tmp_path)]

    assert order == ["printer.cfg", "generated/one.cfg", "generated/two.cfg", "orphan.cfg"]


def test_get_cfg_loading_overview_reports_klipper_vs_scan_order(tmp_path: Path) -> None:
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

    overview = get_cfg_loading_overview(tmp_path)

    klipper_order = [row["file_path"] for row in overview["klipper_order"]]
    klipper_macro_order = overview["klipper_macro_order"]

    assert klipper_order == ["printer.cfg", "extras/b.cfg", "extras/a.cfg"]
    assert overview["klipper_count"] == 3
    assert klipper_macro_order == []
    assert overview["klipper_macro_count"] == 0
    assert "klippervault_order" not in overview
    assert "klippervault_count" not in overview


def test_get_cfg_loading_overview_ignores_dynamicmacros_section_like_klipper(tmp_path: Path) -> None:
    _write(
        tmp_path / "printer.cfg",
        """
        [dynamicmacros]
        configs: generated.cfg
        """,
    )
    _write(tmp_path / "generated.cfg", "[printer]\n")

    overview = get_cfg_loading_overview(tmp_path)

    klipper_order = [row["file_path"] for row in overview["klipper_order"]]
    assert klipper_order == ["printer.cfg"]
    assert overview["klipper_count"] == 1


def test_get_cfg_loading_overview_preserves_duplicate_include_entries_like_klipper(tmp_path: Path) -> None:
    _write(
        tmp_path / "printer.cfg",
        """
        [include extras/a.cfg]
        [include extras/b.cfg]
        """,
    )
    _write(
        tmp_path / "extras" / "a.cfg",
        """
        [include common.cfg]
        """,
    )
    _write(
        tmp_path / "extras" / "b.cfg",
        """
        [include common.cfg]
        """,
    )
    _write(tmp_path / "extras" / "common.cfg", "[printer]\n")

    overview = get_cfg_loading_overview(tmp_path)
    klipper_order = [row["file_path"] for row in overview["klipper_order"]]

    assert klipper_order == [
        "printer.cfg",
        "extras/a.cfg",
        "extras/common.cfg",
        "extras/b.cfg",
        "extras/common.cfg",
    ]


def test_get_cfg_loading_overview_reports_macro_level_inline_include_order(tmp_path: Path) -> None:
        _write(
                tmp_path / "printer.cfg",
                """
                [gcode_macro PRINTER_HEAD]
                gcode:
                    RESPOND MSG="head"

                [include macros.cfg]

                [gcode_macro PRINTER_TAIL]
                gcode:
                    RESPOND MSG="tail"
                """,
        )
        _write(
                tmp_path / "macros.cfg",
                """
                [gcode_macro BEFORE_INCLUDE]
                gcode:
                    RESPOND MSG="before"

                [include extras/sub.cfg]

                [gcode_macro AFTER_INCLUDE]
                gcode:
                    RESPOND MSG="after"
                """,
        )
        _write(
                tmp_path / "extras" / "sub.cfg",
                """
                [gcode_macro SUB_MACRO]
                gcode:
                    RESPOND MSG="sub"
                """,
        )

        overview = get_cfg_loading_overview(tmp_path)

        assert [row["file_path"] for row in overview["klipper_order"]] == [
                "printer.cfg",
                "macros.cfg",
                "extras/sub.cfg",
        ]
        assert [row["macro_name"] for row in overview["klipper_macro_order"]] == [
                "PRINTER_HEAD",
                "BEFORE_INCLUDE",
                "SUB_MACRO",
                "AFTER_INCLUDE",
                "PRINTER_TAIL",
        ]
        assert overview["klipper_macro_count"] == 5
        assert overview["klipper_macro_order"][2]["file_path"] == "extras/sub.cfg"
        assert overview["klipper_macro_order"][2]["line_number"] == 1


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


def test_run_indexing_respects_inline_include_order_for_active_macro(tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        db_path = tmp_path / "db" / "macros.db"
        _write(
                config_dir / "printer.cfg",
                """
                [include parent.cfg]
                """,
        )
        _write(
                config_dir / "parent.cfg",
                """
                [include nested.cfg]

                [gcode_macro HELLO]
                gcode:
                    RESPOND MSG="parent after include"
                """,
        )
        _write(
                config_dir / "nested.cfg",
                """
                [gcode_macro HELLO]
                gcode:
                    RESPOND MSG="nested"
                """,
        )

        run_indexing(config_dir, db_path)
        macros = load_macro_list(db_path, config_dir=config_dir)
        rows_by_path = {row["file_path"]: row for row in macros}

        assert rows_by_path["nested.cfg"]["is_active"] is False
        assert rows_by_path["parent.cfg"]["is_active"] is True


def test_run_indexing_marks_unreferenced_cfg_macros_not_loaded(tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        db_path = tmp_path / "db" / "macros.db"
        _write(
                config_dir / "printer.cfg",
                """
                [include loaded.cfg]
                """,
        )
        _write(
                config_dir / "loaded.cfg",
                """
                [gcode_macro HELLO]
                gcode:
                    RESPOND MSG="loaded"
                """,
        )
        _write(
                config_dir / "orphan.cfg",
                """
                [gcode_macro HELLO]
                gcode:
                    RESPOND MSG="orphan"
                """,
        )

        result = run_indexing(config_dir, db_path)
        macros = load_macro_list(db_path)
        rows_by_path = {row["file_path"]: row for row in macros}

        assert result["cfg_files_scanned"] == 3
        assert set(rows_by_path) == {"loaded.cfg", "orphan.cfg"}
        assert rows_by_path["loaded.cfg"]["is_loaded"] is True
        assert rows_by_path["loaded.cfg"]["is_dynamic"] is False
        assert rows_by_path["loaded.cfg"]["is_active"] is True
        assert rows_by_path["orphan.cfg"]["is_loaded"] is False
        assert rows_by_path["orphan.cfg"]["is_dynamic"] is False
        assert rows_by_path["orphan.cfg"]["is_active"] is False


def test_run_indexing_treats_dynamicmacros_configs_as_loaded(tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        db_path = tmp_path / "db" / "macros.db"
        _write(
                config_dir / "printer.cfg",
                """
                [dynamicmacros]
                configs: generated.cfg
                """,
        )
        _write(
                config_dir / "generated.cfg",
                """
                [gcode_macro HELLO]
                gcode:
                    RESPOND MSG="generated"
                """,
        )
        _write(
                config_dir / "orphan.cfg",
                """
                [gcode_macro BYE]
                gcode:
                    RESPOND MSG="orphan"
                """,
        )

        result = run_indexing(config_dir, db_path)
        macros = load_macro_list(db_path)
        rows_by_path = {row["file_path"]: row for row in macros}

        assert result["cfg_files_scanned"] == 3
        assert set(rows_by_path) == {"generated.cfg", "orphan.cfg"}
        assert rows_by_path["generated.cfg"]["is_loaded"] is True
        assert rows_by_path["generated.cfg"]["is_dynamic"] is True
        assert rows_by_path["generated.cfg"]["is_active"] is True
        assert rows_by_path["orphan.cfg"]["is_loaded"] is False
        assert rows_by_path["orphan.cfg"]["is_dynamic"] is False
        assert rows_by_path["orphan.cfg"]["is_active"] is False


def test_run_indexing_reports_dynamic_insert_count(tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        db_path = tmp_path / "db" / "macros.db"
        _write(
                config_dir / "printer.cfg",
                """
                [dynamicmacros]
                configs: generated.cfg
                [include static.cfg]
                """,
        )
        _write(
                config_dir / "generated.cfg",
                """
                [gcode_macro DYN_HELLO]
                gcode:
                    RESPOND MSG="dyn"
                """,
        )
        _write(
                config_dir / "static.cfg",
                """
                [gcode_macro STATIC_HELLO]
                gcode:
                    RESPOND MSG="static"
                """,
        )

        result = run_indexing(config_dir, db_path)

        assert result["macros_inserted"] == 2
        assert result["dynamic_macros_inserted"] == 1


def test_run_indexing_ignores_dot_dynamicmacros_cfg(tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        db_path = tmp_path / "db" / "macros.db"
        _write(
                config_dir / "printer.cfg",
                """
                [include loaded.cfg]
                """,
        )
        _write(
                config_dir / "loaded.cfg",
                """
                [gcode_macro HELLO]
                gcode:
                    RESPOND MSG="loaded"
                """,
        )
        _write(
                config_dir / ".dynamicmacros.cfg",
                """
                [gcode_macro SHOULD_BE_IGNORED]
                gcode:
                    RESPOND MSG="ignored"
                """,
        )

        result = run_indexing(config_dir, db_path)
        macros = load_macro_list(db_path)

        assert result["cfg_files_scanned"] == 2
        assert {row["macro_name"] for row in macros} == {"HELLO"}


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


def test_load_duplicate_macro_groups_scoped_by_printer_profile(tmp_path: Path) -> None:
    db_path = tmp_path / "db" / "macros.db"

    config_p1 = tmp_path / "config_p1"
    _write(
        config_p1 / "printer.cfg",
        """
        [include base.cfg]
        [include override.cfg]
        """,
    )
    _write(
        config_p1 / "base.cfg",
        """
        [gcode_macro HELLO]
        gcode:
          RESPOND MSG="base"
        """,
    )
    _write(
        config_p1 / "override.cfg",
        """
        [gcode_macro HELLO]
        gcode:
          RESPOND MSG="override"
        """,
    )

    config_p2 = tmp_path / "config_p2"
    _write(
        config_p2 / "printer.cfg",
        """
        [gcode_macro HELLO]
        gcode:
          RESPOND MSG="single"
        """,
    )

    run_indexing(config_p1, db_path, printer_profile_id=1)
    run_indexing(config_p2, db_path, printer_profile_id=2)

    scoped_p1 = load_duplicate_macro_groups(db_path, printer_profile_id=1)
    scoped_p2 = load_duplicate_macro_groups(db_path, printer_profile_id=2)

    assert len(scoped_p1) == 1
    assert scoped_p1[0]["macro_name"] == "HELLO"
    assert len(scoped_p1[0]["entries"]) == 2
    assert scoped_p2 == []


def test_remove_inactive_macro_version_scoped_by_printer_profile(tmp_path: Path) -> None:
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

    run_indexing(config_dir, db_path, printer_profile_id=1)
    run_indexing(config_dir, db_path, printer_profile_id=2)

    p1_base = next(row for row in load_macro_list(db_path, printer_profile_id=1) if row["file_path"] == "base.cfg")

    result = remove_inactive_macro_version(
        db_path,
        "base.cfg",
        "HELLO",
        int(p1_base["version"]),
        printer_profile_id=1,
    )

    p1_rows = load_macro_list(db_path, printer_profile_id=1)
    p2_rows = load_macro_list(db_path, printer_profile_id=2)
    assert result == {"removed": 1, "reason": "removed"}
    assert len(p1_rows) == 1
    assert len(p2_rows) == 2


def test_remove_deleted_macro_scoped_by_printer_profile(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    db_path = tmp_path / "db" / "macros.db"
    _write(
        config_dir / "printer.cfg",
        """
        [gcode_macro HELLO]
        gcode:
          RESPOND MSG="hello"
        """,
    )

    run_indexing(config_dir, db_path, printer_profile_id=1)
    run_indexing(config_dir, db_path, printer_profile_id=2)

    with open_sqlite_connection(db_path) as conn:
        conn.execute("UPDATE macros SET is_deleted = 1 WHERE printer_profile_id = 1")
        conn.execute("UPDATE macros SET is_deleted = 1 WHERE printer_profile_id = 2")
        conn.commit()

    result = remove_deleted_macro(db_path, "printer.cfg", "HELLO", printer_profile_id=1)

    p1_rows = load_macro_list(db_path, printer_profile_id=1)
    p2_rows = load_macro_list(db_path, printer_profile_id=2)
    assert result == {"removed": 1, "reason": "removed"}
    assert p1_rows == []
    assert len(p2_rows) == 1
    assert p2_rows[0]["is_deleted"] is True


def test_restore_macro_version_scoped_by_printer_profile(tmp_path: Path) -> None:
    db_path = tmp_path / "db" / "macros.db"

    config_p1 = tmp_path / "config_p1"
    _write(
        config_p1 / "printer.cfg",
        """
        [gcode_macro HELLO]
        gcode:
          RESPOND MSG="profile-1"
        """,
    )
    config_p2 = tmp_path / "config_p2"
    _write(
        config_p2 / "printer.cfg",
        """
        [gcode_macro HELLO]
        gcode:
          RESPOND MSG="profile-2"
        """,
    )

    run_indexing(config_p1, db_path, printer_profile_id=1)
    run_indexing(config_p2, db_path, printer_profile_id=2)

    restore_dir = tmp_path / "restore_target"
    restore_macro_version(
        db_path,
        restore_dir,
        "printer.cfg",
        "HELLO",
        1,
        printer_profile_id=2,
    )

    restored = (restore_dir / "printer.cfg").read_text(encoding="utf-8")
    assert "profile-2" in restored
    assert "profile-1" not in restored


def test_export_and_import_share_marks_rows_new_and_inactive(tmp_path: Path) -> None:
    source_config_dir = tmp_path / "source_config"
    source_db_path = tmp_path / "source_db" / "macros.db"
    _write(
        source_config_dir / "printer.cfg",
        """
        [gcode_macro HELLO]
        description: shared hello
        gcode:
          RESPOND MSG="hello"
        """,
    )
    run_indexing(source_config_dir, source_db_path)

    payload = export_macro_share_payload(
        db_path=source_db_path,
        identities=[("printer.cfg", "HELLO")],
        source_vendor="Voron",
        source_model="V2.4",
    )

    assert payload["format"] == "klippervault.macro-share.v1"
    assert payload["source_printer"] == {"vendor": "Voron", "model": "V2.4"}
    assert len(payload["macros"]) == 1

    target_db_path = tmp_path / "target_db" / "macros.db"
    import_result = import_macro_share_payload(target_db_path, payload)
    assert import_result["imported"] == 1

    imported_rows = load_macro_list(target_db_path)
    assert len(imported_rows) == 1
    imported_row = imported_rows[0]
    assert imported_row["macro_name"] == "HELLO"
    assert imported_row["is_active"] is False
    assert imported_row["is_loaded"] is False
    assert imported_row["is_new"] is True

    target_config_dir = tmp_path / "target_config"
    restore_result = restore_macro_version(
        db_path=target_db_path,
        config_dir=target_config_dir,
        file_path=str(imported_row["file_path"]),
        macro_name="HELLO",
        version=int(imported_row["version"]),
    )

    assert restore_result["file_path"] == "macros.cfg"
    restored_cfg = (target_config_dir / "macros.cfg").read_text(encoding="utf-8")
    assert "[gcode_macro HELLO]" in restored_cfg
    printer_cfg = (target_config_dir / "printer.cfg").read_text(encoding="utf-8")
    assert "[include macros.cfg]" in printer_cfg


def test_reindex_keeps_unactivated_imported_macros_marked_new(tmp_path: Path) -> None:
        source_config_dir = tmp_path / "source_config"
        source_db_path = tmp_path / "source_db" / "macros.db"
        _write(
                source_config_dir / "printer.cfg",
                """
                [gcode_macro HELLO]
                gcode:
                    RESPOND MSG="hello"

                [gcode_macro BYE]
                gcode:
                    RESPOND MSG="bye"
                """,
        )
        run_indexing(source_config_dir, source_db_path)

        payload = export_macro_share_payload(
                db_path=source_db_path,
                identities=[("printer.cfg", "HELLO"), ("printer.cfg", "BYE")],
                source_vendor="Voron",
                source_model="V2.4",
        )

        target_db_path = tmp_path / "target_db" / "macros.db"
        import_macro_share_payload(target_db_path, payload)

        imported_rows = load_macro_list(target_db_path)
        hello_row = next(row for row in imported_rows if row["macro_name"] == "HELLO")

        target_config_dir = tmp_path / "target_config"
        restore_macro_version(
                db_path=target_db_path,
                config_dir=target_config_dir,
                file_path=str(hello_row["file_path"]),
                macro_name="HELLO",
                version=int(hello_row["version"]),
        )

        run_indexing(target_config_dir, target_db_path)

        rows_after_index = load_macro_list(target_db_path)
        bye_row = next(row for row in rows_after_index if row["macro_name"] == "BYE")

        assert bye_row["is_new"] is True
        assert bye_row["is_deleted"] is False
        assert bye_row["is_active"] is False


def test_load_macro_list_marks_identity_new_when_older_pending_version_exists(tmp_path: Path) -> None:
    target_config_dir = tmp_path / "target_config"
    target_db_path = tmp_path / "target_db" / "macros.db"
    _write(
        target_config_dir / "printer.cfg",
        """
        [gcode_macro HELLO]
        description: current hello
        gcode:
          RESPOND MSG="current"
        """,
    )
    run_indexing(target_config_dir, target_db_path)

    import_online_macro_updates(
        target_db_path,
        updates=[
            {
                "identity": "voron::v2.4::HELLO",
                "macro_name": "HELLO",
                "source_vendor": "Voron",
                "source_model": "V2.4",
                "source_file_path": "printer.cfg",
                "section_text": textwrap.dedent(
                    """
                    [gcode_macro HELLO]
                    description: imported hello
                    gcode:
                      RESPOND MSG="imported"
                    """
                ).lstrip("\n"),
                "remote_path": "voron/v2.4/HELLO.json",
                "remote_version": "2026-04-07",
            }
        ],
        repo_url="https://github.com/example/klipper-macros",
        repo_ref="main",
    )

    # Re-index the unchanged local cfg so the original on-disk version becomes
    # latest again while the imported version remains pending in history.
    run_indexing(target_config_dir, target_db_path)

    macros = load_macro_list(target_db_path)
    assert len(macros) == 1
    assert macros[0]["macro_name"] == "HELLO"
    assert macros[0]["is_new"] is True
    assert macros[0]["is_active"] is True


def test_load_macro_list_attaches_true_macro_load_order_when_config_dir_given(tmp_path: Path) -> None:
    """load_macro_list enriches each macro with true macro-level load order."""
    config_dir = tmp_path / "config"
    db_path = tmp_path / "db" / "macros.db"
    _write(
        config_dir / "printer.cfg",
                "[gcode_macro PRINTER_MACRO]\n"
                "gcode:\n"
                "  RESPOND MSG=\"from printer\"\n\n"
                "[include macros.cfg]\n\n"
                "[gcode_macro PRINTER_TAIL_MACRO]\n"
                "gcode:\n"
                "  RESPOND MSG=\"after include in printer\"\n",
    )
    _write(
        config_dir / "macros.cfg",
                "[gcode_macro BEFORE_INCLUDE]\n"
                "gcode:\n"
                "  RESPOND MSG=\"before\"\n\n"
                "[include extras/sub.cfg]\n\n"
                "[gcode_macro AFTER_INCLUDE_MACRO]\n"
                "gcode:\n"
                "  RESPOND MSG=\"after\"\n",
    )
    _write(
        config_dir / "extras" / "sub.cfg",
        """
        [gcode_macro SUB_MACRO]
        gcode:
          RESPOND MSG="from sub"
        """,
    )
    run_indexing(config_dir, db_path)

    macros = load_macro_list(db_path, config_dir=config_dir)
    by_name = {row["macro_name"]: row for row in macros}

    assert by_name["PRINTER_MACRO"]["load_order_index"] == 0
    assert by_name["BEFORE_INCLUDE"]["load_order_index"] == 1
    assert by_name["SUB_MACRO"]["load_order_index"] == 2
    assert by_name["AFTER_INCLUDE_MACRO"]["load_order_index"] == 3
    assert by_name["PRINTER_TAIL_MACRO"]["load_order_index"] == 4

    macros_by_load_order = sorted(macros, key=lambda row: row["load_order_index"])
    assert [row["macro_name"] for row in macros_by_load_order] == [
        "PRINTER_MACRO",
        "BEFORE_INCLUDE",
        "SUB_MACRO",
        "AFTER_INCLUDE_MACRO",
        "PRINTER_TAIL_MACRO",
    ]


def test_load_macro_list_attaches_load_order_with_config_source(tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        db_path = tmp_path / "db" / "macros.db"
        _write(
                config_dir / "printer.cfg",
                """
                [gcode_macro ROOT_MACRO]
                gcode:
                    RESPOND MSG="root"

                [include macros.cfg]
                """,
        )
        _write(
                config_dir / "macros.cfg",
                """
                [gcode_macro INCLUDED_MACRO]
                gcode:
                    RESPOND MSG="included"
                """,
        )
        run_indexing(config_dir, db_path)

        source = LocalConfigSource(root_dir=config_dir)
        macros = load_macro_list(db_path, config_source=source)
        by_name = {row["macro_name"]: row for row in macros}

        assert by_name["ROOT_MACRO"]["load_order_index"] == 0
        assert by_name["INCLUDED_MACRO"]["load_order_index"] == 1


def test_get_cfg_load_order_from_source_matches_path_order(tmp_path: Path) -> None:
    _write(
        tmp_path / "printer.cfg",
        """
        [include extras/b.cfg]
        [include extras/a.cfg]
        """,
    )
    _write(tmp_path / "extras" / "a.cfg", "[printer]\n")
    _write(tmp_path / "extras" / "b.cfg", "[printer]\n")

    source = LocalConfigSource(root_dir=tmp_path)
    order = get_cfg_load_order_from_source(source)

    assert [path.name for path in order] == ["printer.cfg", "b.cfg", "a.cfg"]


def test_get_cfg_loading_overview_from_source_reports_expected_order(tmp_path: Path) -> None:
    _write(
        tmp_path / "printer.cfg",
        """
        [include macros.cfg]
        """,
    )
    _write(
        tmp_path / "macros.cfg",
        """
        [gcode_macro PRINT_START]
        gcode:
          RESPOND MSG="ok"
        """,
    )

    source = LocalConfigSource(root_dir=tmp_path)
    overview = get_cfg_loading_overview_from_source(source)

    assert [row["file_path"] for row in overview["klipper_order"]] == ["printer.cfg", "macros.cfg"]
    assert overview["klipper_macro_count"] == 1
    assert overview["klipper_macro_order"][0]["macro_name"] == "PRINT_START"


def test_get_cfg_loading_overview_from_source_preserves_duplicate_include_entries_like_klipper(tmp_path: Path) -> None:
    _write(
        tmp_path / "printer.cfg",
        """
        [include extras/a.cfg]
        [include extras/b.cfg]
        """,
    )
    _write(
        tmp_path / "extras" / "a.cfg",
        """
        [include common.cfg]
        """,
    )
    _write(
        tmp_path / "extras" / "b.cfg",
        """
        [include common.cfg]
        """,
    )
    _write(tmp_path / "extras" / "common.cfg", "[printer]\n")

    source = LocalConfigSource(root_dir=tmp_path)
    overview = get_cfg_loading_overview_from_source(source)

    assert [row["file_path"] for row in overview["klipper_order"]] == [
        "printer.cfg",
        "extras/a.cfg",
        "extras/common.cfg",
        "extras/b.cfg",
        "extras/common.cfg",
    ]


def test_run_indexing_from_source_indexes_cfg_tree(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    db_path = tmp_path / "db" / "vault.db"
    _write(
        config_dir / "printer.cfg",
        """
        [gcode_macro PRINT_START]
        gcode:
          RESPOND MSG="from-source"
        """,
    )

    source = LocalConfigSource(root_dir=config_dir)
    result = run_indexing_from_source(source, db_path)
    macros = load_macro_list(db_path)

    assert result["cfg_files_scanned"] == 1
    assert result["macros_inserted"] == 1
    assert len(macros) == 1
    assert macros[0]["macro_name"] == "PRINT_START"


def test_run_indexing_from_source_treats_dynamicmacros_configs_as_loaded(tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        db_path = tmp_path / "db" / "vault.db"
        _write(
                config_dir / "printer.cfg",
                """
                [dynamicmacros]
                configs: generated.cfg
                """,
        )
        _write(
                config_dir / "generated.cfg",
                """
                [gcode_macro HELLO]
                gcode:
                    RESPOND MSG="generated"
                """,
        )
        _write(
                config_dir / "orphan.cfg",
                """
                [gcode_macro BYE]
                gcode:
                    RESPOND MSG="orphan"
                """,
        )

        source = LocalConfigSource(root_dir=config_dir)
        result = run_indexing_from_source(source, db_path)
        macros = load_macro_list(db_path)
        rows_by_path = {row["file_path"]: row for row in macros}

        assert result["cfg_files_scanned"] == 3
        assert rows_by_path["generated.cfg"]["is_loaded"] is True
        assert rows_by_path["generated.cfg"]["is_dynamic"] is True
        assert rows_by_path["generated.cfg"]["is_active"] is True
        assert rows_by_path["orphan.cfg"]["is_loaded"] is False
        assert rows_by_path["orphan.cfg"]["is_dynamic"] is False


def test_run_indexing_from_source_ignores_dot_dynamicmacros_cfg(tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        db_path = tmp_path / "db" / "vault.db"
        _write(
                config_dir / "printer.cfg",
                """
                [include loaded.cfg]
                """,
        )
        _write(
                config_dir / "loaded.cfg",
                """
                [gcode_macro HELLO]
                gcode:
                    RESPOND MSG="loaded"
                """,
        )
        _write(
                config_dir / ".dynamicmacros.cfg",
                """
                [gcode_macro SHOULD_BE_IGNORED]
                gcode:
                    RESPOND MSG="ignored"
                """,
        )

        source = LocalConfigSource(root_dir=config_dir)
        result = run_indexing_from_source(source, db_path)
        macros = load_macro_list(db_path)

        assert result["cfg_files_scanned"] == 2
        assert {row["macro_name"] for row in macros} == {"HELLO"}


def test_load_order_from_source_stays_available_with_ignored_dot_dynamicmacros_include(tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        db_path = tmp_path / "db" / "vault.db"
        _write(
                config_dir / "printer.cfg",
                """
                [include .dynamicmacros.cfg]
                [include macros.cfg]

                [gcode_macro ROOT]
                gcode:
                    RESPOND MSG="root"
                """,
        )
        _write(
                config_dir / "macros.cfg",
                """
                [gcode_macro CHILD]
                gcode:
                    RESPOND MSG="child"
                """,
        )
        _write(
                config_dir / ".dynamicmacros.cfg",
                """
                [gcode_macro IGNORED]
                gcode:
                    RESPOND MSG="ignored"
                """,
        )

        source = LocalConfigSource(root_dir=config_dir)
        run_indexing_from_source(source, db_path)
        macros = load_macro_list(db_path, config_source=source)
        by_name = {row["macro_name"]: row for row in macros}

        assert set(by_name) == {"CHILD", "ROOT"}
        assert by_name["CHILD"]["load_order_index"] == 0
        assert by_name["ROOT"]["load_order_index"] == 1
