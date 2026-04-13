#!/usr/bin/env python3
# Copyright (C) 2026 Juergen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Database-backed printer profile storage for multi-printer workflows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time

from klipper_vault_db import open_sqlite_connection


@dataclass
class PrinterProfile:
    """One printer profile used to scope macro history and connection metadata."""

    profile_name: str
    vendor: str = ""
    model: str = ""
    connection_type: str = "off_printer"
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_username: str = ""
    ssh_remote_config_dir: str = ""
    ssh_moonraker_url: str = ""
    ssh_auth_mode: str = "key"
    ssh_credential_ref: str = ""
    ssh_profile_id: int | None = None
    is_active: bool = False
    is_archived: bool = False
    id: int | None = None


def ensure_printer_profile_schema(conn) -> None:
    """Ensure schema for printer profile records."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS printer_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_name TEXT NOT NULL UNIQUE,
            vendor TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            connection_type TEXT NOT NULL DEFAULT 'off_printer',
            ssh_host TEXT NOT NULL DEFAULT '',
            ssh_port INTEGER NOT NULL DEFAULT 22,
            ssh_username TEXT NOT NULL DEFAULT '',
            ssh_remote_config_dir TEXT NOT NULL DEFAULT '',
            ssh_moonraker_url TEXT NOT NULL DEFAULT '',
            ssh_auth_mode TEXT NOT NULL DEFAULT 'key',
            ssh_credential_ref TEXT NOT NULL DEFAULT '',
            ssh_profile_id INTEGER,
            is_active INTEGER NOT NULL DEFAULT 0,
            is_archived INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY (ssh_profile_id)
                REFERENCES ssh_host_profiles (id)
                ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_printer_profiles_single_active
        ON printer_profiles (is_active)
        WHERE is_active = 1
        """
    )
    existing_cols = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(printer_profiles)").fetchall()
    }
    column_defs = {
        "ssh_host": "TEXT NOT NULL DEFAULT ''",
        "ssh_port": "INTEGER NOT NULL DEFAULT 22",
        "ssh_username": "TEXT NOT NULL DEFAULT ''",
        "ssh_remote_config_dir": "TEXT NOT NULL DEFAULT ''",
        "ssh_moonraker_url": "TEXT NOT NULL DEFAULT ''",
        "ssh_auth_mode": "TEXT NOT NULL DEFAULT 'key'",
        "ssh_credential_ref": "TEXT NOT NULL DEFAULT ''",
    }
    for col_name, col_def in column_defs.items():
        if col_name not in existing_cols:
            conn.execute(f"ALTER TABLE printer_profiles ADD COLUMN {col_name} {col_def}")


def list_printer_profiles(db_path: Path, *, include_archived: bool = False) -> list[PrinterProfile]:
    """Return saved printer profiles."""
    with open_sqlite_connection(db_path, ensure_schema=ensure_printer_profile_schema) as conn:
        if include_archived:
            rows = conn.execute(
                """
                SELECT
                    id,
                    profile_name,
                    vendor,
                    model,
                    connection_type,
                    ssh_host,
                    ssh_port,
                    ssh_username,
                    ssh_remote_config_dir,
                    ssh_moonraker_url,
                    ssh_auth_mode,
                    ssh_credential_ref,
                    ssh_profile_id,
                    is_active,
                    is_archived
                FROM printer_profiles
                ORDER BY is_active DESC, profile_name COLLATE NOCASE ASC
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT
                    id,
                    profile_name,
                    vendor,
                    model,
                    connection_type,
                    ssh_host,
                    ssh_port,
                    ssh_username,
                    ssh_remote_config_dir,
                    ssh_moonraker_url,
                    ssh_auth_mode,
                    ssh_credential_ref,
                    ssh_profile_id,
                    is_active,
                    is_archived
                FROM printer_profiles
                WHERE is_archived = 0
                ORDER BY is_active DESC, profile_name COLLATE NOCASE ASC
                """
            ).fetchall()

    return [
        PrinterProfile(
            id=int(row[0]),
            profile_name=str(row[1]),
            vendor=str(row[2] or ""),
            model=str(row[3] or ""),
            connection_type=str(row[4] or "off_printer"),
            ssh_host=str(row[5] or ""),
            ssh_port=int(row[6] or 22),
            ssh_username=str(row[7] or ""),
            ssh_remote_config_dir=str(row[8] or ""),
            ssh_moonraker_url=str(row[9] or ""),
            ssh_auth_mode=str(row[10] or "key"),
            ssh_credential_ref=str(row[11] or ""),
            ssh_profile_id=int(row[12]) if row[12] is not None else None,
            is_active=bool(row[13]),
            is_archived=bool(row[14]),
        )
        for row in rows
    ]


def get_active_printer_profile(db_path: Path) -> PrinterProfile | None:
    """Return currently active printer profile."""
    with open_sqlite_connection(db_path, ensure_schema=ensure_printer_profile_schema) as conn:
        row = conn.execute(
            """
            SELECT
                id,
                profile_name,
                vendor,
                model,
                connection_type,
                ssh_host,
                ssh_port,
                ssh_username,
                ssh_remote_config_dir,
                ssh_moonraker_url,
                ssh_auth_mode,
                ssh_credential_ref,
                ssh_profile_id,
                is_active,
                is_archived
            FROM printer_profiles
            WHERE is_active = 1
            LIMIT 1
            """
        ).fetchone()

    if row is None:
        return None

    return PrinterProfile(
        id=int(row[0]),
        profile_name=str(row[1]),
        vendor=str(row[2] or ""),
        model=str(row[3] or ""),
        connection_type=str(row[4] or "off_printer"),
        ssh_host=str(row[5] or ""),
        ssh_port=int(row[6] or 22),
        ssh_username=str(row[7] or ""),
        ssh_remote_config_dir=str(row[8] or ""),
        ssh_moonraker_url=str(row[9] or ""),
        ssh_auth_mode=str(row[10] or "key"),
        ssh_credential_ref=str(row[11] or ""),
        ssh_profile_id=int(row[12]) if row[12] is not None else None,
        is_active=bool(row[13]),
        is_archived=bool(row[14]),
    )


def ensure_default_printer_profile(db_path: Path) -> int:
    """Ensure one default active printer profile exists and return its id."""
    active = get_active_printer_profile(db_path)
    if active is not None and active.id is not None:
        return int(active.id)

    with open_sqlite_connection(db_path, ensure_schema=ensure_printer_profile_schema) as conn:
        now = int(time.time())
        row = conn.execute(
            """
            SELECT id
            FROM printer_profiles
            WHERE is_archived = 0
            ORDER BY id ASC
            LIMIT 1
            """
        ).fetchone()

        if row is None:
            cursor = conn.execute(
                """
                INSERT INTO printer_profiles (
                    profile_name, vendor, model, connection_type, ssh_profile_id,
                    is_active, is_archived, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("Default Printer", "", "", "off_printer", None, 1, 0, now, now),
            )
            profile_id_raw = cursor.lastrowid
            if profile_id_raw is None:
                raise RuntimeError("failed to insert default printer profile")
            profile_id = int(profile_id_raw)
            conn.commit()
            return profile_id

        row_id = row[0]
        if row_id is None:
            raise RuntimeError("invalid printer_profiles row without id")
        profile_id = int(row_id)
        conn.execute("UPDATE printer_profiles SET is_active = 0 WHERE is_active = 1")
        conn.execute(
            "UPDATE printer_profiles SET is_active = 1, updated_at = ? WHERE id = ?",
            (now, profile_id),
        )
        conn.commit()
        return profile_id


def set_active_printer_profile(db_path: Path, profile_id: int) -> bool:
    """Activate one profile and deactivate all others."""
    with open_sqlite_connection(db_path, ensure_schema=ensure_printer_profile_schema) as conn:
        row = conn.execute(
            "SELECT id FROM printer_profiles WHERE id = ? AND is_archived = 0",
            (int(profile_id),),
        ).fetchone()
        if row is None:
            return False

        now = int(time.time())
        conn.execute("UPDATE printer_profiles SET is_active = 0 WHERE is_active = 1")
        conn.execute(
            "UPDATE printer_profiles SET is_active = 1, updated_at = ? WHERE id = ?",
            (now, int(profile_id)),
        )
        conn.commit()
        return True


def get_printer_profile_by_ssh_profile_id(db_path: Path, ssh_profile_id: int) -> PrinterProfile | None:
    """Return first non-archived profile linked to one SSH profile id."""
    with open_sqlite_connection(db_path, ensure_schema=ensure_printer_profile_schema) as conn:
        row = conn.execute(
            """
            SELECT
                id,
                profile_name,
                vendor,
                model,
                connection_type,
                ssh_host,
                ssh_port,
                ssh_username,
                ssh_remote_config_dir,
                ssh_moonraker_url,
                ssh_auth_mode,
                ssh_credential_ref,
                ssh_profile_id,
                is_active,
                is_archived
            FROM printer_profiles
            WHERE ssh_profile_id = ? AND is_archived = 0
            ORDER BY id ASC
            LIMIT 1
            """,
            (int(ssh_profile_id),),
        ).fetchone()
    if row is None:
        return None
    return PrinterProfile(
        id=int(row[0]),
        profile_name=str(row[1]),
        vendor=str(row[2] or ""),
        model=str(row[3] or ""),
        connection_type=str(row[4] or "off_printer"),
        ssh_host=str(row[5] or ""),
        ssh_port=int(row[6] or 22),
        ssh_username=str(row[7] or ""),
        ssh_remote_config_dir=str(row[8] or ""),
        ssh_moonraker_url=str(row[9] or ""),
        ssh_auth_mode=str(row[10] or "key"),
        ssh_credential_ref=str(row[11] or ""),
        ssh_profile_id=int(row[12]) if row[12] is not None else None,
        is_active=bool(row[13]),
        is_archived=bool(row[14]),
    )


def create_printer_profile(
    db_path: Path,
    *,
    profile_name: str,
    vendor: str = "",
    model: str = "",
    connection_type: str = "off_printer",
    ssh_profile_id: int | None = None,
    is_active: bool = False,
) -> int:
    """Create one printer profile row and return its id."""
    now = int(time.time())
    normalized_name = str(profile_name or "").strip() or "Printer"
    normalized_connection_type = "off_printer"

    with open_sqlite_connection(db_path, ensure_schema=ensure_printer_profile_schema) as conn:
        if is_active:
            conn.execute("UPDATE printer_profiles SET is_active = 0 WHERE is_active = 1")
        cursor = conn.execute(
            """
            INSERT INTO printer_profiles (
                profile_name, vendor, model, connection_type, ssh_profile_id,
                is_active, is_archived, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_name,
                str(vendor or "").strip(),
                str(model or "").strip(),
                normalized_connection_type,
                int(ssh_profile_id) if ssh_profile_id is not None else None,
                1 if is_active else 0,
                0,
                now,
                now,
            ),
        )
        profile_id_raw = cursor.lastrowid
        if profile_id_raw is None:
            raise RuntimeError("failed to insert printer profile")
        profile_id = int(profile_id_raw)
        conn.commit()
        return profile_id


def update_printer_profile_identity(
    db_path: Path,
    *,
    profile_id: int,
    vendor: str,
    model: str,
) -> bool:
    """Update vendor/model for one profile."""
    with open_sqlite_connection(db_path, ensure_schema=ensure_printer_profile_schema) as conn:
        updated = conn.execute(
            """
            UPDATE printer_profiles
            SET vendor = ?, model = ?, updated_at = ?
            WHERE id = ? AND is_archived = 0
            """,
            (str(vendor or "").strip(), str(model or "").strip(), int(time.time()), int(profile_id)),
        ).rowcount
        conn.commit()
        return bool(updated)


def update_printer_profile_connection(
    db_path: Path,
    *,
    profile_id: int,
    host: str,
    port: int,
    username: str,
    remote_config_dir: str,
    moonraker_url: str,
    auth_mode: str,
    credential_ref: str,
    ssh_profile_id: int | None = None,
) -> bool:
    """Update SSH connection settings owned by one printer profile."""
    with open_sqlite_connection(db_path, ensure_schema=ensure_printer_profile_schema) as conn:
        updated = conn.execute(
            """
            UPDATE printer_profiles
            SET connection_type = ?,
                ssh_host = ?,
                ssh_port = ?,
                ssh_username = ?,
                ssh_remote_config_dir = ?,
                ssh_moonraker_url = ?,
                ssh_auth_mode = ?,
                ssh_credential_ref = ?,
                ssh_profile_id = ?,
                updated_at = ?
            WHERE id = ? AND is_archived = 0
            """,
            (
                "off_printer",
                str(host or "").strip(),
                max(1, min(65535, int(port))),
                str(username or "").strip(),
                str(remote_config_dir or "").strip(),
                str(moonraker_url or "").strip(),
                (str(auth_mode or "key").strip().lower() or "key"),
                str(credential_ref or "").strip(),
                int(ssh_profile_id) if ssh_profile_id is not None else None,
                int(time.time()),
                int(profile_id),
            ),
        ).rowcount
        conn.commit()
        return bool(updated)
