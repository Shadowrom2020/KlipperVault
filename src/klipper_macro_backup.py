#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Backup helpers for storing and retrieving macro snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from klipper_vault_config_source import ConfigSource
from klipper_vault_db import open_sqlite_connection


_LATEST_VERSION_SUBQUERY = """
SELECT file_path, macro_name, MAX(version) AS max_version
FROM macros
GROUP BY file_path, macro_name
""".strip()

_DB_BATCH_SIZE = 500


def _is_printer_cfg_path(rel_path: str) -> bool:
    """Return True when a backup relative path points to printer.cfg."""
    return Path(str(rel_path or "")).name.lower() == "printer.cfg"


def _cfg_text_contains_gcode_macros(file_text: str) -> bool:
    """Return True when cfg text contains at least one [gcode_macro ...] section."""
    for raw_line in str(file_text or "").splitlines():
        line = raw_line.strip()
        if not (line.startswith("[") and line.endswith("]")):
            continue
        section = line[1:-1].strip().lower()
        if section.startswith("gcode_macro "):
            return True
    return False


def backup_printer_cfg_restore_policy(
    db_path: Path,
    backup_id: int,
    printer_profile_id: int = 1,
) -> Dict[str, object]:
    """Return whether restoring one backup should overwrite printer.cfg."""
    has_printer_cfg_snapshot = False
    printer_cfg_contains_macros = False
    has_file_snapshot = False

    with open_sqlite_connection(db_path, ensure_schema=ensure_backup_schema) as conn:
        has_file_snapshot = bool(
            conn.execute(
                "SELECT 1 FROM macro_backup_files WHERE backup_id = ? AND printer_profile_id = ? LIMIT 1",
                (int(backup_id), int(printer_profile_id)),
            ).fetchone()
        )

        if has_file_snapshot:
            row = conn.execute(
                """
                SELECT file_content
                FROM macro_backup_files
                WHERE backup_id = ? AND printer_profile_id = ? AND lower(file_path) = 'printer.cfg'
                LIMIT 1
                """,
                (int(backup_id), int(printer_profile_id)),
            ).fetchone()
            if row is not None:
                has_printer_cfg_snapshot = True
                printer_cfg_contains_macros = _cfg_text_contains_gcode_macros(str(row[0]))
        else:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM macro_backup_items
                WHERE backup_id = ?
                  AND printer_profile_id = ?
                  AND lower(file_path) = 'printer.cfg'
                  AND is_active = 1
                """,
                (int(backup_id), int(printer_profile_id)),
            ).fetchone()
            printer_cfg_macro_count = int(row[0]) if row is not None else 0
            has_printer_cfg_snapshot = printer_cfg_macro_count > 0
            printer_cfg_contains_macros = printer_cfg_macro_count > 0

    will_overwrite_printer_cfg = has_printer_cfg_snapshot and printer_cfg_contains_macros
    return {
        "has_file_snapshot": has_file_snapshot,
        "has_printer_cfg_snapshot": has_printer_cfg_snapshot,
        "printer_cfg_contains_macros": printer_cfg_contains_macros,
        "will_overwrite_printer_cfg": will_overwrite_printer_cfg,
    }


def _iter_cursor_batches(cursor: sqlite3.Cursor, batch_size: int = _DB_BATCH_SIZE) -> Iterable[list[tuple]]:
    """Yield cursor rows in fixed-size batches."""
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            return
        yield rows


def ensure_backup_schema(conn: sqlite3.Connection) -> None:
    """Ensure backup-related tables and indexes exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS macro_backups (
            id          INTEGER PRIMARY KEY,
            backup_name TEXT    NOT NULL,
            printer_profile_id INTEGER NOT NULL DEFAULT 1,
            created_at  INTEGER NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS macro_backup_items (
            id             INTEGER PRIMARY KEY,
            backup_id      INTEGER NOT NULL,
            printer_profile_id INTEGER NOT NULL DEFAULT 1,
            section_type   TEXT,
            macro_name     TEXT    NOT NULL,
            file_path      TEXT    NOT NULL,
            version        INTEGER NOT NULL,
            indexed_at     INTEGER NOT NULL,
            line_number    INTEGER NOT NULL,
            description    TEXT,
            gcode          TEXT,
            variables_json TEXT    NOT NULL,
            body_checksum  TEXT,
            is_active      INTEGER NOT NULL,
            FOREIGN KEY (backup_id) REFERENCES macro_backups(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS macro_backup_files (
            id           INTEGER PRIMARY KEY,
            backup_id    INTEGER NOT NULL,
            printer_profile_id INTEGER NOT NULL DEFAULT 1,
            file_path    TEXT    NOT NULL,
            file_content TEXT    NOT NULL,
            FOREIGN KEY (backup_id) REFERENCES macro_backups(id) ON DELETE CASCADE
        )
        """
    )

    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(macro_backup_items)")}
    backup_cols = {row[1] for row in conn.execute("PRAGMA table_info(macro_backups)")}
    backup_file_cols = {row[1] for row in conn.execute("PRAGMA table_info(macro_backup_files)")}
    if backup_cols and "printer_profile_id" not in backup_cols:
        conn.execute("ALTER TABLE macro_backups ADD COLUMN printer_profile_id INTEGER NOT NULL DEFAULT 1")
    if existing_cols and "section_type" not in existing_cols:
        conn.execute("ALTER TABLE macro_backup_items ADD COLUMN section_type TEXT")
    if existing_cols and "body_checksum" not in existing_cols:
        conn.execute("ALTER TABLE macro_backup_items ADD COLUMN body_checksum TEXT")
    if existing_cols and "printer_profile_id" not in existing_cols:
        conn.execute("ALTER TABLE macro_backup_items ADD COLUMN printer_profile_id INTEGER NOT NULL DEFAULT 1")
    if backup_file_cols and "printer_profile_id" not in backup_file_cols:
        conn.execute("ALTER TABLE macro_backup_files ADD COLUMN printer_profile_id INTEGER NOT NULL DEFAULT 1")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_backup_items_backup_id "
        "ON macro_backup_items(backup_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_backup_items_profile_id "
        "ON macro_backup_items(printer_profile_id, backup_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_backups_profile_id "
        "ON macro_backups(printer_profile_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_backup_files_backup_id "
        "ON macro_backup_files(backup_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_backup_files_profile_id "
        "ON macro_backup_files(printer_profile_id, backup_id)"
    )


def _iter_cfg_files(config_dir: Path) -> List[Path]:
    """Return all cfg files under config_dir in deterministic order."""
    files: List[Path] = []
    for root, _, names in os.walk(config_dir):
        for name in names:
            if name.lower().endswith(".cfg"):
                files.append(Path(root) / name)
    files.sort(key=lambda p: str(p))
    return files


def _safe_cfg_path(config_dir: Path, rel_path: str) -> Path:
    """Return safe absolute file path inside config_dir for a relative cfg path."""
    candidate = (config_dir / rel_path).resolve()
    config_root = config_dir.resolve()
    if candidate != config_root and config_root not in candidate.parents:
        raise ValueError(f"invalid backup file path outside config directory: {rel_path}")
    return candidate


def create_macro_backup(
    db_path: Path,
    backup_name: str,
    config_dir: Optional[Path] = None,
    config_source: ConfigSource | None = None,
    now_ts: Optional[int] = None,
    printer_profile_id: int = 1,
) -> Dict[str, object]:
    """Snapshot the latest row of every macro into a named backup set."""
    name = backup_name.strip()
    if not name:
        raise ValueError("backup name must not be empty")
    if config_dir is None and config_source is None:
        raise ValueError("backup aborted: config_dir or config_source is required for fully restorable backups")

    ts = now_ts if now_ts is not None else int(time.time())

    with open_sqlite_connection(
        db_path,
        ensure_schema=ensure_backup_schema,
        pragmas=("PRAGMA foreign_keys=ON",),
    ) as conn:

        insert_result = conn.execute(
            "INSERT INTO macro_backups (backup_name, printer_profile_id, created_at) VALUES (?, ?, ?)",
            (name, int(printer_profile_id), ts),
        )
        backup_id_raw = insert_result.lastrowid
        if backup_id_raw is None:
            raise RuntimeError("failed to create backup row")
        backup_id = int(backup_id_raw)

        has_macros_table = bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='macros' LIMIT 1"
            ).fetchone()
        )

        macro_count = 0
        if has_macros_table:
            select_cursor = conn.execute(
                f"""
                SELECT
                    m.section_type,
                    m.macro_name,
                    m.file_path,
                    m.version,
                    m.indexed_at,
                    m.line_number,
                    m.description,
                    m.gcode,
                    m.variables_json,
                    m.body_checksum,
                    m.is_active
                FROM macros AS m
                INNER JOIN (
                    {_LATEST_VERSION_SUBQUERY}
                ) AS latest
                    ON m.file_path = latest.file_path
                   AND m.macro_name = latest.macro_name
                   AND m.version = latest.max_version
                WHERE m.printer_profile_id = ?
                ORDER BY m.macro_name COLLATE NOCASE ASC, m.file_path ASC
                """,
                (int(printer_profile_id),),
            )
            for chunk in _iter_cursor_batches(select_cursor):
                conn.executemany(
                    """
                    INSERT INTO macro_backup_items (
                        backup_id,
                        printer_profile_id,
                        section_type,
                        macro_name,
                        file_path,
                        version,
                        indexed_at,
                        line_number,
                        description,
                        gcode,
                        variables_json,
                        body_checksum,
                        is_active
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            backup_id,
                            int(printer_profile_id),
                            str(section_type),
                            str(macro_name),
                            str(file_path),
                            int(version),
                            int(indexed_at),
                            int(line_number),
                            description,
                            gcode,
                            str(variables_json),
                            str(body_checksum),
                            int(is_active),
                        )
                        for section_type, macro_name, file_path, version, indexed_at, line_number, description, gcode, variables_json, body_checksum, is_active in chunk
                    ],
                )
                macro_count += len(chunk)

        cfg_files_count = 0
        if config_source is not None:
            cfg_rows: List[tuple[int, int, str, str]] = []
            cfg_files = sorted({str(path) for path in config_source.list_cfg_files() if str(path).lower().endswith(".cfg")})

            # File-level backups must always include printer.cfg when available.
            if "printer.cfg" not in cfg_files:
                printer_cfg_fallback_added = False
                if config_dir is not None:
                    printer_cfg_local = config_dir / "printer.cfg"
                    if printer_cfg_local.exists() and printer_cfg_local.is_file():
                        cfg_rows.append(
                            (
                                backup_id,
                                int(printer_profile_id),
                                "printer.cfg",
                                printer_cfg_local.read_text(encoding="utf-8", errors="ignore"),
                            )
                        )
                        cfg_files_count += 1
                        printer_cfg_fallback_added = True
                if not printer_cfg_fallback_added:
                    try:
                        printer_cfg_content = config_source.read_text("printer.cfg")
                    except Exception:
                        printer_cfg_content = None
                    if printer_cfg_content is not None:
                        cfg_rows.append((backup_id, int(printer_profile_id), "printer.cfg", printer_cfg_content))
                        cfg_files_count += 1

            for rel_path in cfg_files:
                file_content = config_source.read_text(str(rel_path))
                cfg_rows.append((backup_id, int(printer_profile_id), str(rel_path), file_content))
                cfg_files_count += 1
                if len(cfg_rows) >= _DB_BATCH_SIZE:
                    conn.executemany(
                        """
                        INSERT INTO macro_backup_files (backup_id, printer_profile_id, file_path, file_content)
                        VALUES (?, ?, ?, ?)
                        """,
                        cfg_rows,
                    )
                    cfg_rows.clear()
            if cfg_rows:
                conn.executemany(
                    """
                    INSERT INTO macro_backup_files (backup_id, printer_profile_id, file_path, file_content)
                    VALUES (?, ?, ?, ?)
                    """,
                    cfg_rows,
                )
            if cfg_files_count == 0:
                raise ValueError("backup aborted: no .cfg files found to snapshot")
        elif config_dir is not None and config_dir.exists() and config_dir.is_dir():
            cfg_file_rows: List[tuple[int, int, str, str]] = []
            for cfg_file in _iter_cfg_files(config_dir):
                rel_path = str(cfg_file.relative_to(config_dir))
                file_content = cfg_file.read_text(encoding="utf-8", errors="ignore")
                cfg_file_rows.append((backup_id, int(printer_profile_id), rel_path, file_content))
                cfg_files_count += 1
                if len(cfg_file_rows) >= _DB_BATCH_SIZE:
                    conn.executemany(
                        """
                        INSERT INTO macro_backup_files (backup_id, printer_profile_id, file_path, file_content)
                        VALUES (?, ?, ?, ?)
                        """,
                        cfg_file_rows,
                    )
                    cfg_file_rows.clear()
            if cfg_file_rows:
                conn.executemany(
                    """
                    INSERT INTO macro_backup_files (backup_id, printer_profile_id, file_path, file_content)
                    VALUES (?, ?, ?, ?)
                    """,
                    cfg_file_rows,
                )
            if cfg_files_count == 0:
                raise ValueError("backup aborted: no .cfg files found to snapshot")
            if not (config_dir / "printer.cfg").exists():
                raise ValueError("backup aborted: printer.cfg not found")
        else:
            raise ValueError(f"backup aborted: config directory not found: {config_dir}")

        conn.commit()

    return {
        "backup_id": backup_id,
        "backup_name": name,
        "created_at": ts,
        "macro_count": macro_count,
        "cfg_file_count": cfg_files_count,
    }


def list_macro_backups(db_path: Path, limit: int = 200, printer_profile_id: int | None = None) -> List[Dict[str, object]]:
    """Return available backups, newest first."""
    with open_sqlite_connection(db_path, ensure_schema=ensure_backup_schema) as conn:
        rows = conn.execute(
            """
            SELECT
                b.id,
                b.backup_name,
                b.created_at,
                COUNT(i.id) AS macro_count
            FROM macro_backups AS b
            LEFT JOIN macro_backup_items AS i
                ON i.backup_id = b.id
            WHERE (? IS NULL OR b.printer_profile_id = ?)
            GROUP BY b.id, b.backup_name, b.created_at
            ORDER BY b.created_at DESC, b.id DESC
            LIMIT ?
            """,
            (printer_profile_id, printer_profile_id, limit),
        ).fetchall()

    return [
        {
            "backup_id": int(backup_id),
            "backup_name": str(backup_name),
            "created_at": int(created_at),
            "macro_count": int(macro_count),
        }
        for backup_id, backup_name, created_at, macro_count in rows
    ]


def load_backup_items(db_path: Path, backup_id: int, printer_profile_id: int | None = None) -> List[Dict[str, object]]:
    """Load the macro rows stored in one backup snapshot."""
    with open_sqlite_connection(db_path, ensure_schema=ensure_backup_schema) as conn:
        rows = conn.execute(
            """
            SELECT
                section_type,
                macro_name,
                file_path,
                version,
                indexed_at,
                line_number,
                description,
                gcode,
                variables_json,
                body_checksum,
                is_active
            FROM macro_backup_items
            WHERE backup_id = ?
                            AND (? IS NULL OR printer_profile_id = ?)
            ORDER BY macro_name COLLATE NOCASE ASC, file_path ASC
            """,
                        (int(backup_id), printer_profile_id, printer_profile_id),
        ).fetchall()

    return [
        {
            "section_type": str(section_type) if section_type is not None else None,
            "macro_name": str(macro_name),
            "file_path": str(file_path),
            "version": int(version),
            "indexed_at": int(indexed_at),
            "line_number": int(line_number),
            "description": description,
            "gcode": gcode,
            "variables_json": str(variables_json),
            "body_checksum": str(body_checksum) if body_checksum is not None else None,
            "is_active": bool(is_active),
        }
        for section_type, macro_name, file_path, version, indexed_at, line_number, description, gcode, variables_json, body_checksum, is_active in rows
    ]


def _ensure_macros_schema_for_restore(conn: sqlite3.Connection) -> None:
    """Ensure the macros table exists when restoring before any index run."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS macros (
            id            INTEGER PRIMARY KEY,
            printer_profile_id INTEGER NOT NULL DEFAULT 1,
            file_path     TEXT    NOT NULL,
            section_type  TEXT    NOT NULL,
            macro_name    TEXT    NOT NULL,
            line_number   INTEGER NOT NULL,
            description   TEXT,
            gcode         TEXT,
            variables_json TEXT   NOT NULL,
            body_checksum TEXT    NOT NULL,
            is_active     INTEGER NOT NULL DEFAULT 0,
            version       INTEGER NOT NULL,
            indexed_at    INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_macros_version_profile "
        "ON macros(printer_profile_id, file_path, macro_name, version)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_macros_name ON macros(macro_name)")


def _synthetic_checksum(
    section_type: str,
    macro_name: str,
    file_path: str,
    line_number: int,
    description: Optional[str],
    gcode: Optional[str],
    variables_json: str,
) -> str:
    """Build deterministic checksum for restored rows lacking stored checksum."""
    payload = "\n".join(
        [
            section_type,
            macro_name,
            file_path,
            str(line_number),
            description or "",
            gcode or "",
            variables_json,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()


def _macro_item_to_section_text(
    section_type: str,
    macro_name: str,
    description: Optional[str],
    gcode: Optional[str],
    variables_json: str,
) -> str:
    """Build one cfg section from backup macro item data."""
    header_section = section_type.strip() if section_type.strip() else "gcode_macro"
    lines: List[str] = [f"[{header_section} {macro_name}]\n"]

    if description:
        lines.append(f"description: {description}\n")

    try:
        variables = json.loads(variables_json)
    except (TypeError, json.JSONDecodeError):
        variables = {}
    if isinstance(variables, dict):
        for key in sorted(variables.keys()):
            lines.append(f"variable_{key}: {variables[key]}\n")

    if gcode:
        lines.append("gcode:\n")
        for line in str(gcode).splitlines():
            # Preserve stored gcode text verbatim to avoid whitespace-only
            # diffs when restoring historical data.
            lines.append(f"{line}\n")
    return "".join(lines)


def _reconstruct_cfg_files_from_backup_items(rows: list[tuple]) -> List[tuple[str, str]]:
    """Rebuild cfg file snapshots from backup macro rows for legacy backups."""
    file_sections: Dict[str, List[tuple[int, str, str]]] = {}
    for row in rows:
        section_type, macro_name, file_path, _, line_number, description, gcode, variables_json, _, _ = row
        rel_path = str(file_path)
        file_sections.setdefault(rel_path, []).append(
            (
                int(line_number),
                str(macro_name),
                _macro_item_to_section_text(
                    str(section_type or "gcode_macro"),
                    str(macro_name),
                    description,
                    gcode,
                    str(variables_json),
                ),
            )
        )

    rebuilt: List[tuple[str, str]] = []
    for rel_path, sections in file_sections.items():
        sections.sort(key=lambda item: (item[0], item[1].lower()))
        content = "\n".join(section_text.rstrip("\n") for _, _, section_text in sections) + "\n"
        rebuilt.append((rel_path, content))
    rebuilt.sort(key=lambda item: item[0])
    return rebuilt


def restore_macro_backup(
    db_path: Path,
    backup_id: int,
    config_dir: Optional[Path] = None,
    config_source: ConfigSource | None = None,
    now_ts: Optional[int] = None,
    printer_profile_id: int = 1,
) -> Dict[str, object]:
    """Restore one backup snapshot on cfg file level."""
    ts = int(now_ts) if now_ts is not None else int(time.time())
    macro_count = 0
    restored_cfg_files = 0
    removed_cfg_files = 0
    touched_cfg_files: set[str] = set()
    removed_cfg_paths: set[str] = set()
    printer_cfg_policy = backup_printer_cfg_restore_policy(
        db_path=db_path,
        backup_id=int(backup_id),
        printer_profile_id=int(printer_profile_id),
    )
    overwrite_printer_cfg = bool(printer_cfg_policy.get("will_overwrite_printer_cfg", False))

    with open_sqlite_connection(
        db_path,
        ensure_schema=ensure_backup_schema,
        pragmas=("PRAGMA foreign_keys=ON",),
    ) as conn:

        backup_meta = conn.execute(
            "SELECT id, backup_name FROM macro_backups WHERE id = ? AND printer_profile_id = ?",
            (int(backup_id), int(printer_profile_id)),
        ).fetchone()
        if not backup_meta:
            raise ValueError("backup not found")

        rows_query = """
            SELECT
                section_type,
                macro_name,
                file_path,
                version,
                line_number,
                description,
                gcode,
                variables_json,
                body_checksum,
                is_active
            FROM macro_backup_items
            WHERE backup_id = ?
              AND printer_profile_id = ?
            ORDER BY macro_name COLLATE NOCASE ASC, file_path ASC
            """
        has_file_snapshot = bool(
            conn.execute(
                "SELECT 1 FROM macro_backup_files WHERE backup_id = ? AND printer_profile_id = ? LIMIT 1",
                (int(backup_id), int(printer_profile_id)),
            ).fetchone()
        )

        macro_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM macro_backup_items WHERE backup_id = ? AND printer_profile_id = ?",
                (int(backup_id), int(printer_profile_id)),
            ).fetchone()[0]
        )

        # Legacy backups created before cfg snapshots existed require
        # reconstructing cfg state from all backup macro items.
        legacy_rows: List[tuple] = []
        legacy_file_rows: List[tuple[str, str]] = []
        if (config_dir is not None or config_source is not None) and not has_file_snapshot:
            legacy_rows = conn.execute(rows_query, (int(backup_id), int(printer_profile_id))).fetchall()
            legacy_file_rows = _reconstruct_cfg_files_from_backup_items(legacy_rows)

        if config_source is not None:
            snapshot_paths: set[str] = set()
            if has_file_snapshot:
                file_cursor = conn.execute(
                    """
                    SELECT file_path, file_content
                    FROM macro_backup_files
                    WHERE backup_id = ? AND printer_profile_id = ?
                    ORDER BY file_path ASC
                    """,
                    (int(backup_id), int(printer_profile_id)),
                )
                for chunk in _iter_cursor_batches(file_cursor):
                    for rel_path, content in chunk:
                        rel = str(rel_path)
                        snapshot_paths.add(rel)
                        if _is_printer_cfg_path(rel) and not overwrite_printer_cfg:
                            continue
                        touched_cfg_files.add(rel)
                        config_source.write_text(rel, str(content))
                        restored_cfg_files += 1
            elif legacy_file_rows:
                for rel_path, content in legacy_file_rows:
                    rel = str(rel_path)
                    snapshot_paths.add(rel)
                    if _is_printer_cfg_path(rel) and not overwrite_printer_cfg:
                        continue
                    touched_cfg_files.add(rel)
                    config_source.write_text(rel, str(content))
                    restored_cfg_files += 1

            if snapshot_paths:
                for rel in config_source.list_cfg_files():
                    if _is_printer_cfg_path(rel) and not overwrite_printer_cfg:
                        continue
                    if rel not in snapshot_paths and config_source.remove(rel):
                        removed_cfg_files += 1
                        removed_cfg_paths.add(rel)
        elif config_dir is not None:
            config_dir = config_dir.expanduser().resolve()
            config_dir.mkdir(parents=True, exist_ok=True)

            snapshot_paths_local: set[str] = set()
            if has_file_snapshot:
                file_cursor = conn.execute(
                    """
                    SELECT file_path, file_content
                    FROM macro_backup_files
                    WHERE backup_id = ? AND printer_profile_id = ?
                    ORDER BY file_path ASC
                    """,
                    (int(backup_id), int(printer_profile_id)),
                )
                for chunk in _iter_cursor_batches(file_cursor):
                    for rel_path, content in chunk:
                        rel = str(rel_path)
                        snapshot_paths_local.add(rel)
                        if _is_printer_cfg_path(rel) and not overwrite_printer_cfg:
                            continue
                        touched_cfg_files.add(rel)
                        target = _safe_cfg_path(config_dir, rel)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(str(content), encoding="utf-8")
                        restored_cfg_files += 1
            elif legacy_file_rows:
                for rel_path, content in legacy_file_rows:
                    rel = str(rel_path)
                    snapshot_paths_local.add(rel)
                    if _is_printer_cfg_path(rel) and not overwrite_printer_cfg:
                        continue
                    touched_cfg_files.add(rel)
                    target = _safe_cfg_path(config_dir, rel)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(str(content), encoding="utf-8")
                    restored_cfg_files += 1

            if snapshot_paths_local:
                existing_cfg = [p for p in _iter_cfg_files(config_dir)]
                for cfg_file in existing_cfg:
                    rel = str(cfg_file.relative_to(config_dir))
                    if _is_printer_cfg_path(rel) and not overwrite_printer_cfg:
                        continue
                    if rel not in snapshot_paths_local:
                        cfg_file.unlink(missing_ok=True)
                        removed_cfg_files += 1
                        removed_cfg_paths.add(rel)

    return {
        "backup_id": int(backup_meta[0]),
        "backup_name": str(backup_meta[1]),
        "restored_at": ts,
        "macro_count": macro_count,
        "restored_cfg_files": restored_cfg_files,
        "removed_cfg_files": removed_cfg_files,
        "touched_cfg_files": sorted(touched_cfg_files),
        "removed_cfg_paths": sorted(removed_cfg_paths),
        "printer_cfg_overwritten": overwrite_printer_cfg,
    }


def delete_macro_backup(db_path: Path, backup_id: int, printer_profile_id: int | None = None) -> Dict[str, object]:
    """Delete one backup and all its snapshot items."""
    with open_sqlite_connection(
        db_path,
        ensure_schema=ensure_backup_schema,
        pragmas=("PRAGMA foreign_keys=ON",),
    ) as conn:

        backup_meta = conn.execute(
            "SELECT id, backup_name FROM macro_backups WHERE id = ? AND (? IS NULL OR printer_profile_id = ?)",
            (int(backup_id), printer_profile_id, printer_profile_id),
        ).fetchone()
        if not backup_meta:
            raise ValueError("backup not found")

        conn.execute("DELETE FROM macro_backups WHERE id = ?", (int(backup_id),))
        conn.commit()

    return {
        "backup_id": int(backup_meta[0]),
        "backup_name": str(backup_meta[1]),
    }
