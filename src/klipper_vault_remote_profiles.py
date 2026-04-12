#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Database-backed remote SSH profile storage for off-printer mode."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time

from klipper_vault_db import open_sqlite_connection


@dataclass
class SshHostProfile:
    """One SSH host profile used to connect to a remote printer."""

    profile_name: str
    host: str
    username: str
    remote_config_dir: str
    moonraker_url: str
    port: int = 22
    auth_mode: str = "key"
    credential_ref: str = ""
    is_active: bool = False
    id: int | None = None


def ensure_remote_profile_schema(conn) -> None:
    """Ensure schema for remote SSH profile metadata and fallback secret store."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ssh_host_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_name TEXT NOT NULL UNIQUE,
            host TEXT NOT NULL,
            port INTEGER NOT NULL DEFAULT 22,
            username TEXT NOT NULL,
            remote_config_dir TEXT NOT NULL,
            moonraker_url TEXT NOT NULL,
            auth_mode TEXT NOT NULL DEFAULT 'key',
            credential_ref TEXT NOT NULL DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ssh_profiles_single_active
        ON ssh_host_profiles (is_active)
        WHERE is_active = 1
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS credential_store_index (
            credential_ref TEXT PRIMARY KEY,
            secret_type TEXT NOT NULL,
            backend TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS credential_fallback_secrets (
            credential_ref TEXT PRIMARY KEY,
            secret_value TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY (credential_ref)
                REFERENCES credential_store_index (credential_ref)
                ON DELETE CASCADE
        )
        """
    )


def upsert_ssh_host_profile(db_path: Path, profile: SshHostProfile) -> int:
    """Insert or update one SSH host profile by profile name."""
    now = int(time.time())
    with open_sqlite_connection(db_path, ensure_schema=ensure_remote_profile_schema) as conn:
        existing = conn.execute(
            "SELECT id FROM ssh_host_profiles WHERE profile_name = ?",
            (profile.profile_name.strip(),),
        ).fetchone()

        if profile.is_active:
            conn.execute("UPDATE ssh_host_profiles SET is_active = 0 WHERE is_active = 1")

        if existing is None:
            cursor = conn.execute(
                """
                INSERT INTO ssh_host_profiles (
                    profile_name,
                    host,
                    port,
                    username,
                    remote_config_dir,
                    moonraker_url,
                    auth_mode,
                    credential_ref,
                    is_active,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.profile_name.strip(),
                    profile.host.strip(),
                    max(1, min(65535, int(profile.port))),
                    profile.username.strip(),
                    profile.remote_config_dir.strip(),
                    profile.moonraker_url.strip(),
                    (profile.auth_mode or "key").strip(),
                    (profile.credential_ref or "").strip(),
                    1 if profile.is_active else 0,
                    now,
                    now,
                ),
            )
            profile_id = int(cursor.lastrowid)
        else:
            profile_id = int(existing[0])
            conn.execute(
                """
                UPDATE ssh_host_profiles
                SET host = ?,
                    port = ?,
                    username = ?,
                    remote_config_dir = ?,
                    moonraker_url = ?,
                    auth_mode = ?,
                    credential_ref = ?,
                    is_active = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    profile.host.strip(),
                    max(1, min(65535, int(profile.port))),
                    profile.username.strip(),
                    profile.remote_config_dir.strip(),
                    profile.moonraker_url.strip(),
                    (profile.auth_mode or "key").strip(),
                    (profile.credential_ref or "").strip(),
                    1 if profile.is_active else 0,
                    now,
                    profile_id,
                ),
            )

        conn.commit()
        return profile_id


def list_ssh_host_profiles(db_path: Path) -> list[SshHostProfile]:
    """Return all configured SSH host profiles."""
    with open_sqlite_connection(db_path, ensure_schema=ensure_remote_profile_schema) as conn:
        rows = conn.execute(
            """
            SELECT id, profile_name, host, port, username, remote_config_dir,
                   moonraker_url, auth_mode, credential_ref, is_active
            FROM ssh_host_profiles
            ORDER BY profile_name COLLATE NOCASE ASC
            """
        ).fetchall()

    profiles: list[SshHostProfile] = []
    for row in rows:
        profiles.append(
            SshHostProfile(
                id=int(row[0]),
                profile_name=str(row[1]),
                host=str(row[2]),
                port=int(row[3]),
                username=str(row[4]),
                remote_config_dir=str(row[5]),
                moonraker_url=str(row[6]),
                auth_mode=str(row[7]),
                credential_ref=str(row[8] or ""),
                is_active=bool(row[9]),
            )
        )
    return profiles


def get_active_ssh_host_profile(db_path: Path) -> SshHostProfile | None:
    """Return currently active SSH host profile, if any."""
    with open_sqlite_connection(db_path, ensure_schema=ensure_remote_profile_schema) as conn:
        row = conn.execute(
            """
            SELECT id, profile_name, host, port, username, remote_config_dir,
                   moonraker_url, auth_mode, credential_ref, is_active
            FROM ssh_host_profiles
            WHERE is_active = 1
            LIMIT 1
            """
        ).fetchone()

    if row is None:
        return None

    return SshHostProfile(
        id=int(row[0]),
        profile_name=str(row[1]),
        host=str(row[2]),
        port=int(row[3]),
        username=str(row[4]),
        remote_config_dir=str(row[5]),
        moonraker_url=str(row[6]),
        auth_mode=str(row[7]),
        credential_ref=str(row[8] or ""),
        is_active=bool(row[9]),
    )


def set_active_ssh_host_profile(db_path: Path, profile_id: int) -> bool:
    """Set one profile active and deactivate all others."""
    with open_sqlite_connection(db_path, ensure_schema=ensure_remote_profile_schema) as conn:
        row = conn.execute(
            "SELECT id FROM ssh_host_profiles WHERE id = ?",
            (profile_id,),
        ).fetchone()
        if row is None:
            return False

        conn.execute("UPDATE ssh_host_profiles SET is_active = 0 WHERE is_active = 1")
        conn.execute(
            "UPDATE ssh_host_profiles SET is_active = 1, updated_at = ? WHERE id = ?",
            (int(time.time()), profile_id),
        )
        conn.commit()
        return True


def delete_ssh_host_profile(db_path: Path, profile_id: int) -> dict[str, object]:
    """Delete one SSH profile and clean unused credential metadata.

    Returns deletion metadata including whether the removed profile was active.
    """
    with open_sqlite_connection(db_path, ensure_schema=ensure_remote_profile_schema) as conn:
        row = conn.execute(
            "SELECT id, credential_ref, is_active FROM ssh_host_profiles WHERE id = ?",
            (int(profile_id),),
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "profile not found"}

        credential_ref = str(row[1] or "").strip()
        was_active = bool(row[2])

        conn.execute("DELETE FROM ssh_host_profiles WHERE id = ?", (int(profile_id),))

        if credential_ref:
            still_used = conn.execute(
                "SELECT 1 FROM ssh_host_profiles WHERE credential_ref = ? LIMIT 1",
                (credential_ref,),
            ).fetchone()
            if still_used is None:
                conn.execute(
                    "DELETE FROM credential_store_index WHERE credential_ref = ?",
                    (credential_ref,),
                )

        conn.commit()

    return {
        "ok": True,
        "profile_id": int(profile_id),
        "credential_ref": credential_ref,
        "was_active": was_active,
    }


def set_credential_backend(
    db_path: Path,
    *,
    credential_ref: str,
    secret_type: str,
    backend: str,
) -> None:
    """Upsert credential backend metadata for one reference id."""
    now = int(time.time())
    with open_sqlite_connection(db_path, ensure_schema=ensure_remote_profile_schema) as conn:
        conn.execute(
            """
            INSERT INTO credential_store_index (credential_ref, secret_type, backend, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(credential_ref) DO UPDATE SET
                secret_type = excluded.secret_type,
                backend = excluded.backend,
                updated_at = excluded.updated_at
            """,
            (credential_ref.strip(), secret_type.strip(), backend.strip(), now),
        )
        conn.commit()


def get_credential_backend(db_path: Path, credential_ref: str) -> str | None:
    """Return backend name for one credential reference."""
    with open_sqlite_connection(db_path, ensure_schema=ensure_remote_profile_schema) as conn:
        row = conn.execute(
            "SELECT backend FROM credential_store_index WHERE credential_ref = ?",
            (credential_ref.strip(),),
        ).fetchone()
    if row is None:
        return None
    return str(row[0])


def set_fallback_secret(db_path: Path, *, credential_ref: str, secret_value: str) -> None:
    """Store one fallback secret in SQLite."""
    now = int(time.time())
    with open_sqlite_connection(db_path, ensure_schema=ensure_remote_profile_schema) as conn:
        conn.execute(
            """
            INSERT INTO credential_fallback_secrets (credential_ref, secret_value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(credential_ref) DO UPDATE SET
                secret_value = excluded.secret_value,
                updated_at = excluded.updated_at
            """,
            (credential_ref.strip(), secret_value, now),
        )
        conn.commit()


def get_fallback_secret(db_path: Path, credential_ref: str) -> str | None:
    """Return fallback secret for one reference id."""
    with open_sqlite_connection(db_path, ensure_schema=ensure_remote_profile_schema) as conn:
        row = conn.execute(
            "SELECT secret_value FROM credential_fallback_secrets WHERE credential_ref = ?",
            (credential_ref.strip(),),
        ).fetchone()
    if row is None:
        return None
    return str(row[0])


def clear_fallback_secret(db_path: Path, credential_ref: str) -> None:
    """Delete fallback secret for one reference id."""
    with open_sqlite_connection(db_path, ensure_schema=ensure_remote_profile_schema) as conn:
        conn.execute(
            "DELETE FROM credential_fallback_secrets WHERE credential_ref = ?",
            (credential_ref.strip(),),
        )
        conn.commit()
