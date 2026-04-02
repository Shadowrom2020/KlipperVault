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
from typing import Dict, List, Optional

from klipper_vault_db import open_sqlite_connection


_LATEST_VERSION_SUBQUERY = """
SELECT file_path, macro_name, MAX(version) AS max_version
FROM macros
GROUP BY file_path, macro_name
""".strip()


def ensure_backup_schema(conn: sqlite3.Connection) -> None:
    """Ensure backup-related tables and indexes exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS macro_backups (
            id          INTEGER PRIMARY KEY,
            backup_name TEXT    NOT NULL,
            created_at  INTEGER NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS macro_backup_items (
            id             INTEGER PRIMARY KEY,
            backup_id      INTEGER NOT NULL,
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
            file_path    TEXT    NOT NULL,
            file_content TEXT    NOT NULL,
            FOREIGN KEY (backup_id) REFERENCES macro_backups(id) ON DELETE CASCADE
        )
        """
    )

    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(macro_backup_items)")}
    if existing_cols and "section_type" not in existing_cols:
        conn.execute("ALTER TABLE macro_backup_items ADD COLUMN section_type TEXT")
    if existing_cols and "body_checksum" not in existing_cols:
        conn.execute("ALTER TABLE macro_backup_items ADD COLUMN body_checksum TEXT")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_backup_items_backup_id "
        "ON macro_backup_items(backup_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_backup_files_backup_id "
        "ON macro_backup_files(backup_id)"
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
    now_ts: Optional[int] = None,
) -> Dict[str, object]:
    """Snapshot the latest row of every macro into a named backup set."""
    name = backup_name.strip()
    if not name:
        raise ValueError("backup name must not be empty")
    if config_dir is None:
        raise ValueError("backup aborted: config_dir is required for fully restorable backups")

    ts = int(now_ts) if now_ts is not None else int(time.time())

    with open_sqlite_connection(
        db_path,
        ensure_schema=ensure_backup_schema,
        pragmas=("PRAGMA foreign_keys=ON",),
    ) as conn:

        backup_id = int(
            conn.execute(
                "INSERT INTO macro_backups (backup_name, created_at) VALUES (?, ?)",
                (name, ts),
            ).lastrowid
        )

        has_macros_table = bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='macros' LIMIT 1"
            ).fetchone()
        )

        rows = []
        if has_macros_table:
            rows = conn.execute(
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
                ORDER BY m.macro_name COLLATE NOCASE ASC, m.file_path ASC
                """
            ).fetchall()

        if rows:
            conn.executemany(
                """
                INSERT INTO macro_backup_items (
                    backup_id,
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        backup_id,
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
                    for section_type, macro_name, file_path, version, indexed_at, line_number, description, gcode, variables_json, body_checksum, is_active in rows
                ],
            )

        cfg_files_count = 0
        if config_dir.exists() and config_dir.is_dir():
            cfg_rows = []
            for cfg_file in _iter_cfg_files(config_dir):
                rel_path = str(cfg_file.relative_to(config_dir))
                file_content = cfg_file.read_text(encoding="utf-8", errors="ignore")
                cfg_rows.append((backup_id, rel_path, file_content))
            if cfg_rows:
                conn.executemany(
                    """
                    INSERT INTO macro_backup_files (backup_id, file_path, file_content)
                    VALUES (?, ?, ?)
                    """,
                    cfg_rows,
                )
            cfg_files_count = len(cfg_rows)
            if cfg_files_count == 0:
                raise ValueError("backup aborted: no .cfg files found to snapshot")
        else:
            raise ValueError(f"backup aborted: config directory not found: {config_dir}")

        conn.commit()

    return {
        "backup_id": backup_id,
        "backup_name": name,
        "created_at": ts,
        "macro_count": len(rows),
        "cfg_file_count": cfg_files_count,
    }


def list_macro_backups(db_path: Path, limit: int = 200) -> List[Dict[str, object]]:
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
            GROUP BY b.id, b.backup_name, b.created_at
            ORDER BY b.created_at DESC, b.id DESC
            LIMIT ?
            """,
            (limit,),
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


def load_backup_items(db_path: Path, backup_id: int) -> List[Dict[str, object]]:
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
            ORDER BY macro_name COLLATE NOCASE ASC, file_path ASC
            """,
            (int(backup_id),),
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
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_macros_version "
        "ON macros(file_path, macro_name, version)"
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
    except Exception:
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
    now_ts: Optional[int] = None,
) -> Dict[str, object]:
    """Restore one backup snapshot into the active macros table."""
    ts = int(now_ts) if now_ts is not None else int(time.time())

    with open_sqlite_connection(
        db_path,
        ensure_schema=ensure_backup_schema,
        pragmas=("PRAGMA foreign_keys=ON",),
    ) as conn:
        _ensure_macros_schema_for_restore(conn)

        backup_meta = conn.execute(
            "SELECT id, backup_name FROM macro_backups WHERE id = ?",
            (int(backup_id),),
        ).fetchone()
        if not backup_meta:
            raise ValueError("backup not found")

        rows = conn.execute(
            """
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
            ORDER BY macro_name COLLATE NOCASE ASC, file_path ASC
            """,
            (int(backup_id),),
        ).fetchall()

        file_rows = conn.execute(
            """
            SELECT file_path, file_content
            FROM macro_backup_files
            WHERE backup_id = ?
            ORDER BY file_path ASC
            """,
            (int(backup_id),),
        ).fetchall()

        if config_dir is not None and not file_rows:
            # Legacy backups created before cfg snapshots existed: reconstruct
            # a deterministic cfg state from backed-up macro rows.
            file_rows = _reconstruct_cfg_files_from_backup_items(rows)

        conn.execute("DELETE FROM macros")

        if rows:
            conn.executemany(
                """
                INSERT INTO macros (
                    file_path,
                    section_type,
                    macro_name,
                    line_number,
                    description,
                    gcode,
                    variables_json,
                    body_checksum,
                    is_active,
                    version,
                    indexed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(file_path),
                        str(section_type or "gcode_macro"),
                        str(macro_name),
                        int(line_number),
                        description,
                        gcode,
                        str(variables_json),
                        str(body_checksum)
                        if body_checksum
                        else _synthetic_checksum(
                            str(section_type or "gcode_macro"),
                            str(macro_name),
                            str(file_path),
                            int(line_number),
                            description,
                            gcode,
                            str(variables_json),
                        ),
                        int(is_active),
                        max(1, int(version)),
                        ts,
                    )
                    for section_type, macro_name, file_path, version, line_number, description, gcode, variables_json, body_checksum, is_active in rows
                ],
            )

        conn.commit()

    restored_cfg_files = 0
    removed_cfg_files = 0
    if config_dir is not None and file_rows:
        config_dir = config_dir.expanduser().resolve()
        config_dir.mkdir(parents=True, exist_ok=True)

        snapshot_paths: set[str] = set()
        for rel_path, content in file_rows:
            rel = str(rel_path)
            snapshot_paths.add(rel)
            target = _safe_cfg_path(config_dir, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(content), encoding="utf-8")
            restored_cfg_files += 1

        existing_cfg = [p for p in _iter_cfg_files(config_dir)]
        for cfg_file in existing_cfg:
            rel = str(cfg_file.relative_to(config_dir))
            if rel not in snapshot_paths:
                cfg_file.unlink(missing_ok=True)
                removed_cfg_files += 1

    return {
        "backup_id": int(backup_meta[0]),
        "backup_name": str(backup_meta[1]),
        "restored_at": ts,
        "macro_count": len(rows),
        "restored_cfg_files": restored_cfg_files,
        "removed_cfg_files": removed_cfg_files,
    }


def delete_macro_backup(db_path: Path, backup_id: int) -> Dict[str, object]:
    """Delete one backup and all its snapshot items."""
    with open_sqlite_connection(
        db_path,
        ensure_schema=ensure_backup_schema,
        pragmas=("PRAGMA foreign_keys=ON",),
    ) as conn:

        backup_meta = conn.execute(
            "SELECT id, backup_name FROM macro_backups WHERE id = ?",
            (int(backup_id),),
        ).fetchone()
        if not backup_meta:
            raise ValueError("backup not found")

        conn.execute("DELETE FROM macro_backups WHERE id = ?", (int(backup_id),))
        conn.commit()

    return {
        "backup_id": int(backup_meta[0]),
        "backup_name": str(backup_meta[1]),
    }
