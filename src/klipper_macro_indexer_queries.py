#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read/query helpers for indexer-backed macro views and stats."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Callable, Dict, List

from klipper_vault_config_source import ConfigSource
from klipper_vault_db import open_sqlite_connection

_LATEST_VERSION_SUBQUERY = """
SELECT file_path, macro_name, MAX(version) AS max_version
FROM macros
GROUP BY file_path, macro_name
""".strip()

_LATEST_VERSION_WITH_COUNT_SUBQUERY = """
SELECT file_path, macro_name, MAX(version) AS max_version, COUNT(*) AS version_count
FROM macros
GROUP BY file_path, macro_name
""".strip()


def load_stats(
    *,
    db_path: Path,
    ensure_schema: Callable[[sqlite3.Connection], None],
    printer_profile_id: int | None = None,
) -> Dict[str, object]:
    """Return aggregate stats for the stats panel."""
    if not db_path.exists():
        return {
            "total_macros": 0,
            "deleted_macros": 0,
            "distinct_macro_names": 0,
            "distinct_cfg_files": 0,
            "latest_update_ts": None,
            "macros_per_file": [],
        }

    latest_subquery = _LATEST_VERSION_SUBQUERY
    latest_args: tuple[object, ...] = ()
    if printer_profile_id is not None:
        latest_subquery = """
        SELECT file_path, macro_name, MAX(version) AS max_version
        FROM macros
        WHERE printer_profile_id = ?
        GROUP BY file_path, macro_name
        """.strip()
        latest_args = (int(printer_profile_id),)

    row_filter = ""
    row_args: tuple[object, ...] = ()
    if printer_profile_id is not None:
        row_filter = " AND m.printer_profile_id = ?"
        row_args = (int(printer_profile_id),)

    with open_sqlite_connection(db_path, ensure_schema=ensure_schema) as conn:
        total_macros = int(conn.execute(
            f"""
            SELECT COUNT(*)
            FROM (
                SELECT m.file_path, m.macro_name
                FROM macros AS m
                INNER JOIN (
                    {latest_subquery}
                ) AS latest
                    ON m.file_path = latest.file_path
                   AND m.macro_name = latest.macro_name
                   AND m.version = latest.max_version
                WHERE m.is_deleted = 0
                {row_filter}
            )
            """,
            latest_args + row_args,
        ).fetchone()[0])
        deleted_macros = int(conn.execute(
            f"""
            SELECT COUNT(*)
            FROM (
                SELECT m.file_path, m.macro_name
                FROM macros AS m
                INNER JOIN (
                    {latest_subquery}
                ) AS latest
                    ON m.file_path = latest.file_path
                   AND m.macro_name = latest.macro_name
                   AND m.version = latest.max_version
                WHERE m.is_deleted = 1
                {row_filter}
            )
            """,
            latest_args + row_args,
        ).fetchone()[0])
        distinct_macro_names = int(conn.execute(
            f"""
            SELECT COUNT(DISTINCT m.macro_name)
            FROM macros AS m
            INNER JOIN (
                {latest_subquery}
            ) AS latest
                ON m.file_path = latest.file_path
               AND m.macro_name = latest.macro_name
               AND m.version = latest.max_version
            WHERE m.is_deleted = 0
            {row_filter}
            """,
            latest_args + row_args,
        ).fetchone()[0])
        distinct_runtime_macro_names = int(conn.execute(
            f"""
            SELECT COUNT(DISTINCT COALESCE(NULLIF(TRIM(m.runtime_macro_name), ''), m.macro_name))
            FROM macros AS m
            INNER JOIN (
                {latest_subquery}
            ) AS latest
                ON m.file_path = latest.file_path
               AND m.macro_name = latest.macro_name
               AND m.version = latest.max_version
            WHERE m.is_deleted = 0
            {row_filter}
            """,
            latest_args + row_args,
        ).fetchone()[0])
        distinct_cfg_files = int(conn.execute(
            f"""
            SELECT COUNT(DISTINCT m.file_path)
            FROM macros AS m
            INNER JOIN (
                {latest_subquery}
            ) AS latest
                ON m.file_path = latest.file_path
               AND m.macro_name = latest.macro_name
               AND m.version = latest.max_version
            WHERE m.is_deleted = 0
            {row_filter}
            """,
            latest_args + row_args,
        ).fetchone()[0])
        if printer_profile_id is None:
            latest_update_ts = conn.execute("SELECT MAX(indexed_at) FROM macros").fetchone()[0]
        else:
            latest_update_ts = conn.execute(
                "SELECT MAX(indexed_at) FROM macros WHERE printer_profile_id = ?",
                (int(printer_profile_id),),
            ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT m.file_path, COUNT(DISTINCT m.macro_name) AS macro_count
            FROM macros AS m
            INNER JOIN (
                {latest_subquery}
            ) AS latest
                ON m.file_path = latest.file_path
               AND m.macro_name = latest.macro_name
               AND m.version = latest.max_version
            WHERE m.is_deleted = 0
            {row_filter}
            GROUP BY m.file_path
            ORDER BY macro_count DESC, m.file_path ASC
            LIMIT 20
            """,
            latest_args + row_args,
        ).fetchall()
        macros_per_file = [
            {"file_path": str(file_path), "macro_count": int(macro_count)}
            for file_path, macro_count in rows
        ]

    return {
        "total_macros": total_macros,
        "deleted_macros": deleted_macros,
        "distinct_macro_names": distinct_macro_names,
        "distinct_runtime_macro_names": distinct_runtime_macro_names,
        "distinct_cfg_files": distinct_cfg_files,
        "latest_update_ts": int(latest_update_ts) if latest_update_ts is not None else None,
        "macros_per_file": macros_per_file,
    }


def load_macro_list(
    *,
    db_path: Path,
    ensure_schema: Callable[[sqlite3.Connection], None],
    build_macro_load_order_map: Callable[[Path], Dict[tuple[str, str, int], int]],
    build_macro_load_order_map_from_source: Callable[[ConfigSource], Dict[tuple[str, str, int], int]],
    limit: int = 1000,
    offset: int = 0,
    config_dir: Path | None = None,
    config_source: ConfigSource | None = None,
    include_macro_body: bool = True,
    printer_profile_id: int | None = None,
) -> List[Dict[str, object]]:
    """Return latest version row for each macro (list view payload)."""
    if not db_path.exists():
        return []

    latest_subquery = _LATEST_VERSION_WITH_COUNT_SUBQUERY
    latest_args: tuple[object, ...] = ()
    if printer_profile_id is not None:
        latest_subquery = """
        SELECT file_path, macro_name, MAX(version) AS max_version, COUNT(*) AS version_count
        FROM macros
        WHERE printer_profile_id = ?
        GROUP BY file_path, macro_name
        """.strip()
        latest_args = (int(printer_profile_id),)

    load_order_map: Dict[tuple[str, str, int], int] = {}
    if config_source is not None:
        try:
            load_order_map = build_macro_load_order_map_from_source(config_source)
        except (FileNotFoundError, OSError, ValueError):
            pass
    elif config_dir is not None:
        try:
            load_order_map = build_macro_load_order_map(config_dir)
        except (FileNotFoundError, OSError, ValueError):
            pass

    load_order_name_map: Dict[tuple[str, str], int] = {}
    for (mapped_file_path, mapped_macro_name, _mapped_line), mapped_index in load_order_map.items():
        key = (os.path.normpath(str(mapped_file_path)), str(mapped_macro_name))
        previous = load_order_name_map.get(key)
        if previous is None or int(mapped_index) < int(previous):
            load_order_name_map[key] = int(mapped_index)

    with open_sqlite_connection(db_path, ensure_schema=ensure_schema) as conn:
        if include_macro_body:
            body_columns = "m.gcode, m.variables_json"
        else:
            # Keep variable metadata available for safe editor round-trips.
            body_columns = "NULL AS gcode, m.variables_json"

        rows = conn.execute(
            f"""
            SELECT
                m.macro_name,
                m.file_path,
                m.version,
                m.indexed_at,
                m.line_number,
                m.description,
                m.rename_existing,
                {body_columns},
                m.is_active,
                m.runtime_macro_name,
                m.renamed_from,
                m.is_deleted,
                m.is_loaded,
                m.is_dynamic,
                EXISTS(
                    SELECT 1
                    FROM macros AS pending
                    WHERE pending.file_path = m.file_path
                        AND pending.macro_name = m.macro_name
                        AND pending.is_new = 1
                        AND (? IS NULL OR pending.printer_profile_id = ?)
                ) AS has_new_version,
                cnt.version_count
            FROM macros AS m
            INNER JOIN (
                {latest_subquery}
            ) AS cnt
                ON m.file_path = cnt.file_path
               AND m.macro_name = cnt.macro_name
               AND m.version = cnt.max_version
            WHERE (? IS NULL OR m.printer_profile_id = ?)
            ORDER BY m.macro_name COLLATE NOCASE ASC, m.file_path ASC
            LIMIT ?
            OFFSET ?
            """,
            latest_args + (printer_profile_id, printer_profile_id, printer_profile_id, printer_profile_id, limit, offset),
        ).fetchall()

    result_rows: List[Dict[str, object]] = []
    for (
        macro_name,
        file_path,
        version,
        indexed_at,
        line_number,
        description,
        rename_existing,
        gcode,
        variables_json,
        is_active,
        runtime_macro_name,
        renamed_from,
        is_deleted,
        is_loaded,
        is_dynamic,
        has_new_version,
        version_count,
    ) in rows:
        normalized_file_path = os.path.normpath(str(file_path))
        load_order_index = load_order_map.get((normalized_file_path, str(macro_name), int(line_number)))
        if load_order_index is None:
            load_order_index = load_order_name_map.get((normalized_file_path, str(macro_name)), 999999)

        result_rows.append(
            {
                "macro_name": str(macro_name),
                "file_path": str(file_path),
                "version": int(version),
                "indexed_at": int(indexed_at),
                "line_number": int(line_number),
                "description": description,
                "rename_existing": rename_existing,
                "gcode": gcode,
                "variables_json": str(variables_json),
                "is_active": bool(is_active),
                "runtime_macro_name": str(runtime_macro_name or macro_name),
                "renamed_from": renamed_from,
                "display_name": str(runtime_macro_name or macro_name),
                "is_deleted": bool(is_deleted),
                "is_loaded": bool(is_loaded),
                "is_dynamic": bool(is_dynamic),
                "is_new": bool(has_new_version),
                "version_count": int(version_count),
                "load_order_index": int(load_order_index),
            }
        )

    return result_rows


def load_macro_versions(
    *,
    db_path: Path,
    ensure_schema: Callable[[sqlite3.Connection], None],
    file_path: str,
    macro_name: str,
    printer_profile_id: int | None = None,
) -> List[Dict[str, object]]:
    """Load all stored versions for one macro, newest first."""
    if not db_path.exists():
        return []

    with open_sqlite_connection(db_path, ensure_schema=ensure_schema) as conn:
        rows = conn.execute(
            """
            SELECT
                macro_name,
                file_path,
                version,
                indexed_at,
                line_number,
                description,
                rename_existing,
                gcode,
                variables_json,
                is_active,
                runtime_macro_name,
                renamed_from,
                is_deleted,
                is_loaded,
                is_dynamic,
                is_new
            FROM macros
            WHERE file_path = ? AND macro_name = ?
              AND (? IS NULL OR printer_profile_id = ?)
            ORDER BY version DESC
            """,
            (file_path, macro_name, printer_profile_id, printer_profile_id),
        ).fetchall()

    return [
        {
            "macro_name": str(row_macro_name),
            "file_path": str(row_file_path),
            "version": int(version),
            "indexed_at": int(indexed_at),
            "line_number": int(line_number),
            "description": description,
            "rename_existing": rename_existing,
            "gcode": gcode,
            "variables_json": str(variables_json),
            "is_active": bool(is_active),
            "runtime_macro_name": str(runtime_macro_name or row_macro_name),
            "renamed_from": renamed_from,
            "display_name": str(runtime_macro_name or row_macro_name),
            "is_deleted": bool(is_deleted),
            "is_loaded": bool(is_loaded),
            "is_dynamic": bool(is_dynamic),
            "is_new": bool(is_new),
        }
        for (
            row_macro_name,
            row_file_path,
            version,
            indexed_at,
            line_number,
            description,
            rename_existing,
            gcode,
            variables_json,
            is_active,
            runtime_macro_name,
            renamed_from,
            is_deleted,
            is_loaded,
            is_dynamic,
            is_new,
        ) in rows
    ]


def load_duplicate_macro_groups(
    *,
    db_path: Path,
    ensure_schema: Callable[[sqlite3.Connection], None],
    printer_profile_id: int | None = None,
) -> List[Dict[str, object]]:
    """Return duplicate macro definitions grouped by macro_name."""
    if not db_path.exists():
        return []

    where_profile = ""
    params: tuple[object, ...] = ()
    if printer_profile_id is not None:
        where_profile = "\n                  AND m.printer_profile_id = ?"
        params = (int(printer_profile_id),)

    with open_sqlite_connection(db_path, ensure_schema=ensure_schema) as conn:
        rows = conn.execute(
            f"""
            WITH latest AS (
                {_LATEST_VERSION_SUBQUERY}
            ), latest_rows AS (
                SELECT m.*
                FROM macros AS m
                INNER JOIN latest AS l
                    ON m.file_path = l.file_path
                   AND m.macro_name = l.macro_name
                   AND m.version = l.max_version
                WHERE m.is_deleted = 0
                  AND m.is_loaded = 1
{where_profile}
                  AND COALESCE(NULLIF(TRIM(m.runtime_macro_name), ''), m.macro_name) = m.macro_name
            ), duplicated AS (
                SELECT macro_name
                FROM latest_rows
                GROUP BY macro_name
                HAVING COUNT(*) > 1
            )
            SELECT
                m.macro_name,
                m.file_path,
                m.version,
                m.indexed_at,
                m.is_active
            FROM latest_rows AS m
            INNER JOIN duplicated AS d
                ON d.macro_name = m.macro_name
            ORDER BY m.macro_name COLLATE NOCASE ASC, m.file_path ASC
            """,
            params,
        ).fetchall()

    groups: Dict[str, List[Dict[str, object]]] = {}
    for macro_name, file_path, version, indexed_at, is_active in rows:
        key = str(macro_name)
        groups.setdefault(key, []).append(
            {
                "macro_name": key,
                "file_path": str(file_path),
                "version": int(version),
                "indexed_at": int(indexed_at),
                "is_active": bool(is_active),
            }
        )

    return [
        {"macro_name": macro_name, "entries": entries}
        for macro_name, entries in groups.items()
    ]
