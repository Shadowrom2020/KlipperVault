import json
import textwrap
from pathlib import Path

from klipper_macro_online_update import import_online_macro_updates
from klipper_macro_indexer import (
    export_macro_share_payload,
    get_cfg_load_order,
    import_macro_share_payload,
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
