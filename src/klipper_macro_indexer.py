#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Index Klipper macros from .cfg files into a SQLite database.

Designed for constrained systems (e.g. SBCs running 3D printer stacks):
- no external dependencies
- streaming line parsing
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from klipper_macro_backup import ensure_backup_schema
from klipper_vault_db import open_sqlite_connection


@dataclass
class MacroRecord:
    file_path: str
    section_type: str
    macro_name: str
    line_number: int
    description: Optional[str]
    rename_existing: Optional[str]
    gcode: Optional[str]
    variables_json: str
    body_checksum: str


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


def _iter_included_files(file_path: Path, config_dir: Path) -> Iterable[Path]:
    """Yield included cfg files in the same order they appear in file_path."""
    try:
        with file_path.open("r", encoding="utf-8", errors="ignore") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not (line.startswith("[") and line.endswith("]")):
                    continue

                section_name = line[1:-1].strip()
                section_type, section_arg = _section_parts(section_name)
                if section_type != "include" or not section_arg:
                    continue

                include_expr = section_arg.strip().strip('"').strip("'")
                if not include_expr:
                    continue

                include_base = file_path.parent
                include_glob = Path(include_expr)
                if include_glob.is_absolute():
                    pattern = str(include_glob)
                else:
                    pattern = str((include_base / include_expr).resolve())

                include_candidates = sorted(Path(p).resolve() for p in glob.glob(pattern))
                for include_path in include_candidates:
                    if include_path.is_file() and include_path.suffix.lower() == ".cfg":
                        yield include_path
    except FileNotFoundError:
        return


def get_cfg_load_order(config_dir: Path) -> List[Path]:
    """Resolve cfg load order starting from printer.cfg and following [include ...]."""
    root_cfg = (config_dir / "printer.cfg").resolve()
    if not root_cfg.exists() or not root_cfg.is_file():
        # Fall back to deterministic full-tree scan if printer.cfg is missing.
        return sorted((p.resolve() for p in _iter_cfg_files(config_dir)), key=lambda p: str(p))

    ordered: List[Path] = []
    visited: set[Path] = set()

    def visit(path: Path) -> None:
        resolved = path.resolve()
        if resolved in visited:
            return
        visited.add(resolved)
        ordered.append(resolved)
        for included in _iter_included_files(resolved, config_dir):
            visit(included)

    visit(root_cfg)

    # Keep any non-included cfg files discoverable for visibility, deterministically.
    for cfg in sorted((p.resolve() for p in _iter_cfg_files(config_dir)), key=lambda p: str(p)):
        if cfg not in visited:
            ordered.append(cfg)
    return ordered


def _iter_cfg_files(config_dir: Path) -> Iterable[Path]:
    """Yield all cfg files under config_dir recursively."""
    for root, _, files in os.walk(config_dir):
        for file_name in files:
            if file_name.lower().endswith(".cfg"):
                yield Path(root) / file_name


def _parse_key_value(line: str) -> Optional[tuple[str, str]]:
    """Parse one cfg key/value line using ':' or '=' separators."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if ":" in line:
        key, value = line.split(":", 1)
        return key.strip().lower(), value.strip()
    if "=" in line:
        key, value = line.split("=", 1)
        return key.strip().lower(), value.strip()
    return None


def _section_parts(section_name: str) -> tuple[str, Optional[str]]:
    """Split section name into (section_type, optional_argument)."""
    parts = section_name.strip().split(None, 1)
    if not parts:
        return "", None
    section_type = parts[0].lower()
    section_arg = parts[1].strip() if len(parts) > 1 else None
    return section_type, section_arg


def _is_section_header_line(line: str) -> bool:
    """Return True only for real Klipper section header lines.

    Section headers must start at column 0 and be bracketed, e.g.
    [gcode_macro PRINT_START]. This avoids truncating gcode content that
    contains bracket-like text inside macro bodies.
    """
    if not line or line[0] in {" ", "\t"}:
        return False
    stripped = line.strip()
    if not (stripped.startswith("[") and stripped.endswith("]")):
        return False
    inner = stripped[1:-1].strip()
    if not inner:
        return False
    if "[" in inner or "]" in inner:
        return False

    # Only treat lines as section headers when the section type looks like a
    # real Klipper section name, e.g. gcode_macro, delayed_gcode, printer.
    section_type, _ = _section_parts(inner)
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", section_type):
        return False
    return True


def _make_checksum(text: str) -> str:
    """Return SHA256 checksum for section body text."""
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _normalize_gcode_for_match(gcode: Optional[str]) -> str:
    """Normalize gcode text for semantic version matching.

    Restore writes may normalize macro blocks to two-space indentation.
    Treat that representation as equivalent to previously indexed content.
    """
    if not gcode:
        return ""

    lines = str(gcode).splitlines()
    non_empty = [line for line in lines if line.strip()]
    if non_empty and all(line.startswith("  ") for line in non_empty):
        lines = [line[2:] if line.startswith("  ") else line for line in lines]
    return "\n".join(lines)


def _gcode_equivalent(left: Optional[str], right: Optional[str]) -> bool:
    """Return True when gcode differs only by normalization-safe indentation."""
    if left == right:
        return True
    return _normalize_gcode_for_match(left) == _normalize_gcode_for_match(right)


def _is_trailing_gcode_comment_or_blank(line: str) -> bool:
    """Return True for gcode lines that should be trimmed at block end."""
    stripped = line.strip()
    return not stripped or stripped.startswith("#") or stripped.startswith(";")


def parse_macros_from_cfg(file_path: Path, base_dir: Path) -> List[MacroRecord]:
    """Parse all [gcode_macro ...] sections from one cfg file."""
    results: List[MacroRecord] = []
    current_section: Optional[str] = None
    current_section_line = 0
    current_macro_name: Optional[str] = None
    current_body: List[str] = []
    current_description: Optional[str] = None
    current_rename_existing: Optional[str] = None
    current_variables: Dict[str, str] = {}
    in_gcode_block = False
    current_gcode_lines: List[str] = []

    def finalize_section() -> None:
        # Reads from enclosing-scope variables only; no assignment, so nonlocal is not needed.
        if not current_section or not current_macro_name:
            return

        section_type, _ = _section_parts(current_section)
        body_text = "".join(current_body)
        gcode_lines = list(current_gcode_lines)
        # Drop trailing comments/blank lines when there is no following gcode.
        while gcode_lines and _is_trailing_gcode_comment_or_blank(gcode_lines[-1]):
            gcode_lines.pop()
        gcode_text = "\n".join(gcode_lines).rstrip("\n") if gcode_lines else None
        rel_path = str(file_path.relative_to(base_dir))

        results.append(
            MacroRecord(
                file_path=rel_path,
                section_type=section_type,
                macro_name=current_macro_name,
                line_number=current_section_line,
                description=current_description,
                rename_existing=current_rename_existing,
                gcode=gcode_text,
                variables_json=json.dumps(current_variables, separators=(",", ":"), sort_keys=True),
                body_checksum=_make_checksum(body_text),
            )
        )

    with file_path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()

            if _is_section_header_line(line):
                finalize_section()

                current_section = stripped[1:-1].strip()
                current_section_line = line_number
                current_body = [line]
                current_description = None
                current_rename_existing = None
                current_variables = {}
                in_gcode_block = False
                current_gcode_lines = []

                section_type, section_arg = _section_parts(current_section)
                if section_type == "gcode_macro" and section_arg:
                    current_macro_name = section_arg
                else:
                    current_macro_name = None
                continue

            if current_section is None:
                continue

            current_body.append(line)

            if in_gcode_block:
                # In Klipper, gcode content runs until the next section header.
                # Keep collecting raw lines to avoid truncating macros with
                # blank/unindented template lines.
                current_gcode_lines.append(line.rstrip("\n"))
                continue

            pair = _parse_key_value(line)
            if not pair:
                continue

            key, value = pair
            if key == "gcode":
                in_gcode_block = True
                if value:
                    current_gcode_lines.append(value)
            elif key == "description":
                current_description = value or None
            elif key == "rename_existing":
                current_rename_existing = value or None
            elif key.startswith("variable_"):
                current_variables[key[len("variable_") :]] = value

    finalize_section()
    return results


# Default history limit. Also serves as the fallback for CLI invocations that
# don't load a VaultConfig; the GUI overrides this via vault_cfg.version_history_size.
_MAX_VERSIONS = 5


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Ensure main macro and backup schemas exist with migration safety."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(macros)")}

    # v1→v2 migration: the original schema lacked a `version` column.
    # Drop the table and let CREATE TABLE rebuild it from scratch.
    rebuilt = False
    if existing_cols and "version" not in existing_cols:
        conn.execute("DROP TABLE macros")
        conn.execute("DROP INDEX IF EXISTS idx_macros_name")
        rebuilt = True

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS macros (
            id          INTEGER PRIMARY KEY,
            file_path   TEXT    NOT NULL,
            section_type TEXT   NOT NULL,
            macro_name  TEXT    NOT NULL,
            line_number INTEGER NOT NULL,
            description TEXT,
            rename_existing TEXT,
            gcode       TEXT,
            variables_json TEXT NOT NULL,
            body_checksum  TEXT NOT NULL,
            is_active   INTEGER NOT NULL DEFAULT 0,
            runtime_macro_name TEXT,
            renamed_from TEXT,
            is_deleted  INTEGER NOT NULL DEFAULT 0,
            version     INTEGER NOT NULL,
            indexed_at  INTEGER NOT NULL
        )
        """
    )

    # v2→v3: add is_active column to existing rows.
    # Skip when we just rebuilt from scratch — CREATE TABLE already includes it.
    if not rebuilt and existing_cols and "is_active" not in existing_cols:
        conn.execute("ALTER TABLE macros ADD COLUMN is_active INTEGER NOT NULL DEFAULT 0")
    # v3→v4: add is_deleted marker for macros removed from cfg files.
    if not rebuilt and existing_cols and "is_deleted" not in existing_cols:
        conn.execute("ALTER TABLE macros ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0")
    # v4→v5: preserve rename_existing and runtime command names.
    if not rebuilt and existing_cols and "rename_existing" not in existing_cols:
        conn.execute("ALTER TABLE macros ADD COLUMN rename_existing TEXT")
    if not rebuilt and existing_cols and "runtime_macro_name" not in existing_cols:
        conn.execute("ALTER TABLE macros ADD COLUMN runtime_macro_name TEXT")
    if not rebuilt and existing_cols and "renamed_from" not in existing_cols:
        conn.execute("ALTER TABLE macros ADD COLUMN renamed_from TEXT")

    conn.execute(
        """
        UPDATE macros
        SET runtime_macro_name = COALESCE(NULLIF(TRIM(runtime_macro_name), ''), macro_name)
        WHERE runtime_macro_name IS NULL OR TRIM(runtime_macro_name) = ''
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_macros_version "
        "ON macros(file_path, macro_name, version)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_macros_name ON macros(macro_name)")

    ensure_backup_schema(conn)


def _promote_existing_version_to_latest(
    conn: sqlite3.Connection,
    file_path: str,
    macro_name: str,
    from_version: int,
    to_version: int,
) -> None:
    """Move one historical version to latest without creating a new row.

    Keeps row count unchanged by shifting intermediate version numbers down.
    """
    if from_version >= to_version:
        return

    versions = conn.execute(
        """
        SELECT version
        FROM macros
        WHERE file_path = ? AND macro_name = ?
        """,
        (file_path, macro_name),
    ).fetchall()
    if not versions:
        return
    existing_versions = {int(row[0]) for row in versions}
    if from_version not in existing_versions or to_version not in existing_versions:
        return

    # Shift all rows for this macro to a disjoint temporary range, then remap
    # in a single statement. This avoids transient UNIQUE collisions.
    max_abs_version = max(abs(int(row[0])) for row in versions)
    offset = max_abs_version + 1000
    shifted_from = from_version + offset
    shifted_to = to_version + offset

    conn.execute("SAVEPOINT promote_version")
    try:
        conn.execute(
            """
            UPDATE macros
            SET version = version + ?
            WHERE file_path = ? AND macro_name = ?
            """,
            (offset, file_path, macro_name),
        )
        conn.execute(
            """
            UPDATE macros
            SET version = CASE
                WHEN version = ? THEN ?
                WHEN version > ? AND version <= ? THEN version - ? - 1
                ELSE version - ?
            END
            WHERE file_path = ? AND macro_name = ?
            """,
            (
                shifted_from,
                to_version,
                shifted_from,
                shifted_to,
                offset,
                offset,
                file_path,
                macro_name,
            ),
        )
        conn.execute("RELEASE SAVEPOINT promote_version")
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK TO SAVEPOINT promote_version")
        conn.execute("RELEASE SAVEPOINT promote_version")
        raise


def index_macros(
    conn: sqlite3.Connection, records: List[MacroRecord], now_ts: int,
    max_versions: int = _MAX_VERSIONS,
) -> tuple[int, int]:
    """Insert a new version only when parsed macro content truly changed.

    Returns (inserted, unchanged).
    """
    inserted = 0
    unchanged = 0
    for rec in records:
        versions = conn.execute(
            """
            SELECT
                version,
                description,
                rename_existing,
                gcode,
                variables_json,
                section_type,
                line_number,
                body_checksum
            FROM macros
            WHERE file_path = ? AND macro_name = ?
            ORDER BY version ASC
            """,
            (rec.file_path, rec.macro_name),
        ).fetchall()

        latest = versions[-1] if versions else None

        if latest:
            latest_version = int(latest[0])
            stored_description = latest[1]
            stored_rename_existing = latest[2]
            stored_gcode = latest[3]
            stored_variables_json = str(latest[4])
            stored_section_type = str(latest[5])
            stored_line_number = int(latest[6])
            stored_body_checksum = str(latest[7])
            parsed_description = rec.description
            parsed_rename_existing = rec.rename_existing
            parsed_gcode = rec.gcode
            parsed_variables_json = rec.variables_json

            # If the current cfg body exactly matches a historical version,
            # reactivate that version instead of keeping a newer equivalent row
            # or inserting a duplicate version.
            matched_checksum_version: Optional[int] = None
            for row in versions:
                if str(row[7]) == rec.body_checksum:
                    matched_checksum_version = int(row[0])
                    break

            if matched_checksum_version is not None and matched_checksum_version != latest_version:
                _promote_existing_version_to_latest(
                    conn=conn,
                    file_path=rec.file_path,
                    macro_name=rec.macro_name,
                    from_version=matched_checksum_version,
                    to_version=latest_version,
                )
                conn.execute(
                    """
                    UPDATE macros
                    SET line_number = ?,
                        body_checksum = ?,
                        indexed_at = ?
                    WHERE file_path = ? AND macro_name = ? AND version = ?
                    """,
                    (
                        rec.line_number,
                        rec.body_checksum,
                        now_ts,
                        rec.file_path,
                        rec.macro_name,
                        latest_version,
                    ),
                )
                unchanged += 1
                continue

            content_changed = (
                stored_description != parsed_description
                or stored_rename_existing != parsed_rename_existing
                or not _gcode_equivalent(stored_gcode, parsed_gcode)
                or stored_variables_json != parsed_variables_json
                or stored_section_type != rec.section_type
            )

            if not content_changed:
                # Keep newest row metadata in sync without creating a new version.
                # This avoids version churn from non-semantic changes, while still
                # tracking the latest scan timestamp and parser normalization.
                if (
                    stored_body_checksum != rec.body_checksum
                    or stored_line_number != rec.line_number
                ):
                    conn.execute(
                        """
                        UPDATE macros
                        SET line_number = ?,
                            body_checksum = ?,
                            indexed_at = ?
                        WHERE file_path = ? AND macro_name = ? AND version = ?
                        """,
                        (
                            rec.line_number,
                            rec.body_checksum,
                            now_ts,
                            rec.file_path,
                            rec.macro_name,
                            latest_version,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE macros
                        SET indexed_at = ?
                        WHERE file_path = ? AND macro_name = ? AND version = ?
                        """,
                        (now_ts, rec.file_path, rec.macro_name, latest_version),
                    )
                unchanged += 1
                continue

            # If parsed content equals an older stored version, reactivate it by
            # promoting that row to latest instead of inserting a new version.
            matched_version: Optional[int] = None
            for row in versions:
                row_version = int(row[0])
                row_description = row[1]
                row_rename_existing = row[2]
                row_gcode = row[3]
                row_variables_json = str(row[4])
                row_section_type = str(row[5])
                if (
                    row_description == parsed_description
                    and row_rename_existing == parsed_rename_existing
                    and _gcode_equivalent(row_gcode, parsed_gcode)
                    and row_variables_json == parsed_variables_json
                    and row_section_type == rec.section_type
                ):
                    matched_version = row_version
                    break

            if matched_version is not None:
                _promote_existing_version_to_latest(
                    conn=conn,
                    file_path=rec.file_path,
                    macro_name=rec.macro_name,
                    from_version=matched_version,
                    to_version=latest_version,
                )
                conn.execute(
                    """
                    UPDATE macros
                    SET line_number = ?,
                        body_checksum = ?,
                        indexed_at = ?
                    WHERE file_path = ? AND macro_name = ? AND version = ?
                    """,
                    (
                        rec.line_number,
                        rec.body_checksum,
                        now_ts,
                        rec.file_path,
                        rec.macro_name,
                        latest_version,
                    ),
                )
                unchanged += 1
                continue

            # Parsed content changed compared to the newest known version.
            # Store a new version row.

        new_version = (int(latest[0]) + 1) if latest else 1
        conn.execute(
            """
            INSERT INTO macros (
                file_path, section_type, macro_name, line_number,
                description, rename_existing, gcode, variables_json, body_checksum, is_active,
                runtime_macro_name, renamed_from,
                version, indexed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.file_path,
                rec.section_type,
                rec.macro_name,
                rec.line_number,
                rec.description,
                rec.rename_existing,
                rec.gcode,
                rec.variables_json,
                rec.body_checksum,
                0,
                rec.macro_name,
                None,
                new_version,
                now_ts,
            ),
        )
        # Keep only the last max_versions versions; delete anything older.
        conn.execute(
            """
            DELETE FROM macros
            WHERE file_path = ? AND macro_name = ?
              AND version <= (
                SELECT MAX(version) - ? FROM macros
                WHERE file_path = ? AND macro_name = ?
              )
            """,
            (rec.file_path, rec.macro_name, max_versions, rec.file_path, rec.macro_name),
        )
        inserted += 1

    # Mark latest rows as deleted when their macro is no longer present on disk.
    seen_identities = {(rec.file_path, rec.macro_name) for rec in records}
    latest_rows = conn.execute(_LATEST_VERSION_SUBQUERY).fetchall()
    for file_path, macro_name, max_version in latest_rows:
        is_deleted = 0 if (str(file_path), str(macro_name)) in seen_identities else 1
        conn.execute(
            """
            UPDATE macros
            SET is_deleted = ?
            WHERE file_path = ? AND macro_name = ? AND version = ?
            """,
            (is_deleted, str(file_path), str(macro_name), int(max_version)),
        )

    # Determine active runtime command mapping by cfg loading order.
    # A later [gcode_macro X] overrides command X. With rename_existing: Y,
    # the previous X definition becomes callable as Y.
    runtime_target_by_name: Dict[str, tuple[str, str, str]] = {}
    for rec in records:
        prev_target = runtime_target_by_name.get(rec.macro_name.lower())
        rename_target = str(rec.rename_existing or "").strip()
        if rename_target and prev_target is not None:
            runtime_target_by_name[rename_target.lower()] = (
                prev_target[0],
                prev_target[1],
                rename_target,
            )
        runtime_target_by_name[rec.macro_name.lower()] = (
            rec.file_path,
            rec.macro_name,
            rec.macro_name,
        )

    conn.execute(
        """
        UPDATE macros
        SET is_active = 0,
            runtime_macro_name = macro_name,
            renamed_from = NULL
        """
    )

    runtime_names_by_identity: Dict[tuple[str, str], list[str]] = {}
    for file_path, macro_name, runtime_name in runtime_target_by_name.values():
        key = (file_path, macro_name)
        runtime_names = runtime_names_by_identity.setdefault(key, [])
        if runtime_name not in runtime_names:
            runtime_names.append(runtime_name)

    for (file_path, macro_name), runtime_names in runtime_names_by_identity.items():
        selected_runtime = next(
            (name for name in runtime_names if name.lower() == macro_name.lower()),
            sorted(runtime_names, key=lambda name: name.lower())[0],
        )
        renamed_from = macro_name if selected_runtime.lower() != macro_name.lower() else None
        conn.execute(
            """
            UPDATE macros
            SET is_active = 1,
                runtime_macro_name = ?,
                renamed_from = ?
            WHERE file_path = ? AND macro_name = ?
                            AND is_deleted = 0
              AND version = (
                SELECT MAX(version)
                FROM macros
                WHERE file_path = ? AND macro_name = ?
              )
            """,
            (selected_runtime, renamed_from, file_path, macro_name, file_path, macro_name),
        )

    return inserted, unchanged


def run_indexing(
    config_dir: Path, db_path: Path, max_versions: int = _MAX_VERSIONS
) -> Dict[str, object]:
    """Index cfg files into SQLite and return a small run summary."""
    if not config_dir.exists() or not config_dir.is_dir():
        raise FileNotFoundError(f"config directory not found: {config_dir}")

    all_records: List[MacroRecord] = []
    cfg_count = 0
    for cfg_file in get_cfg_load_order(config_dir):
        cfg_count += 1
        all_records.extend(parse_macros_from_cfg(cfg_file, config_dir))

    now_ts = int(time.time())
    with open_sqlite_connection(
        db_path,
        ensure_schema=ensure_schema,
        pragmas=("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL"),
    ) as conn:
        inserted, unchanged = index_macros(conn, all_records, now_ts, max_versions=max_versions)
        conn.commit()

    return {
        "cfg_files_scanned": cfg_count,
        "macros_inserted": inserted,
        "macros_unchanged": unchanged,
        "db_path": str(db_path),
    }


def load_stats(db_path: Path) -> Dict[str, object]:
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

    with open_sqlite_connection(db_path, ensure_schema=ensure_schema) as conn:
        # Count distinct latest, non-deleted macros to reflect current cfg state.
        total_macros = int(conn.execute(
            f"""
            SELECT COUNT(*)
            FROM (
                SELECT m.file_path, m.macro_name
                FROM macros AS m
                INNER JOIN (
                    {_LATEST_VERSION_SUBQUERY}
                ) AS latest
                    ON m.file_path = latest.file_path
                   AND m.macro_name = latest.macro_name
                   AND m.version = latest.max_version
                WHERE m.is_deleted = 0
            )
            """
        ).fetchone()[0])
        deleted_macros = int(conn.execute(
            f"""
            SELECT COUNT(*)
            FROM (
                SELECT m.file_path, m.macro_name
                FROM macros AS m
                INNER JOIN (
                    {_LATEST_VERSION_SUBQUERY}
                ) AS latest
                    ON m.file_path = latest.file_path
                   AND m.macro_name = latest.macro_name
                   AND m.version = latest.max_version
                WHERE m.is_deleted = 1
            )
            """
        ).fetchone()[0])
        distinct_macro_names = int(conn.execute(
            f"""
            SELECT COUNT(DISTINCT m.macro_name)
            FROM macros AS m
            INNER JOIN (
                {_LATEST_VERSION_SUBQUERY}
            ) AS latest
                ON m.file_path = latest.file_path
               AND m.macro_name = latest.macro_name
               AND m.version = latest.max_version
            WHERE m.is_deleted = 0
            """
        ).fetchone()[0])
        distinct_cfg_files = int(conn.execute(
            f"""
            SELECT COUNT(DISTINCT m.file_path)
            FROM macros AS m
            INNER JOIN (
                {_LATEST_VERSION_SUBQUERY}
            ) AS latest
                ON m.file_path = latest.file_path
               AND m.macro_name = latest.macro_name
               AND m.version = latest.max_version
            WHERE m.is_deleted = 0
            """
        ).fetchone()[0])
        latest_update_ts = conn.execute("SELECT MAX(indexed_at) FROM macros").fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT m.file_path, COUNT(DISTINCT m.macro_name) AS macro_count
            FROM macros AS m
            INNER JOIN (
                {_LATEST_VERSION_SUBQUERY}
            ) AS latest
                ON m.file_path = latest.file_path
               AND m.macro_name = latest.macro_name
               AND m.version = latest.max_version
            WHERE m.is_deleted = 0
            GROUP BY m.file_path
            ORDER BY macro_count DESC, m.file_path ASC
            LIMIT 20
            """
        ).fetchall()
        macros_per_file = [
            {"file_path": str(file_path), "macro_count": int(macro_count)}
            for file_path, macro_count in rows
        ]

    return {
        "total_macros": total_macros,
        "deleted_macros": deleted_macros,
        "distinct_macro_names": distinct_macro_names,
        "distinct_cfg_files": distinct_cfg_files,
        "latest_update_ts": int(latest_update_ts) if latest_update_ts is not None else None,
        "macros_per_file": macros_per_file,
    }


def load_macro_list(db_path: Path, limit: int = 1000) -> List[Dict[str, object]]:
    """Return latest version row for each macro (list view payload)."""
    if not db_path.exists():
        return []

    with open_sqlite_connection(db_path, ensure_schema=ensure_schema) as conn:
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
                m.gcode,
                m.variables_json,
                m.is_active,
                m.runtime_macro_name,
                m.renamed_from,
                m.is_deleted,
                cnt.version_count
            FROM macros AS m
            INNER JOIN (
                {_LATEST_VERSION_WITH_COUNT_SUBQUERY}
            ) AS cnt
                ON m.file_path = cnt.file_path
               AND m.macro_name = cnt.macro_name
               AND m.version = cnt.max_version
            ORDER BY m.macro_name COLLATE NOCASE ASC, m.file_path ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [
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
            "version_count": int(version_count),
        }
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
            version_count,
        ) in rows
    ]


def load_macro_versions(db_path: Path, file_path: str, macro_name: str) -> List[Dict[str, object]]:
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
                is_deleted
            FROM macros
            WHERE file_path = ? AND macro_name = ?
            ORDER BY version DESC
            """,
            (file_path, macro_name),
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
        ) in rows
    ]


def load_duplicate_macro_groups(db_path: Path) -> List[Dict[str, object]]:
    """Return duplicate macro definitions grouped by macro_name."""
    if not db_path.exists():
        return []

    with open_sqlite_connection(db_path, ensure_schema=ensure_schema) as conn:
        rows = conn.execute(
            f"""
            WITH latest AS (
                {_LATEST_VERSION_SUBQUERY}
            ), duplicated AS (
                SELECT macro_name
                FROM latest
                GROUP BY macro_name
                HAVING COUNT(*) > 1
            )
            SELECT
                m.macro_name,
                m.file_path,
                m.version,
                m.indexed_at,
                m.is_active
            FROM macros AS m
            INNER JOIN latest AS l
                ON m.file_path = l.file_path
               AND m.macro_name = l.macro_name
               AND m.version = l.max_version
            INNER JOIN duplicated AS d
                ON d.macro_name = m.macro_name
            WHERE m.is_deleted = 0
            ORDER BY m.macro_name COLLATE NOCASE ASC, m.file_path ASC
            """
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


def _safe_cfg_path(config_dir: Path, rel_path: str) -> Path:
    """Return a cfg path safely constrained inside config_dir."""
    candidate = (config_dir / rel_path).resolve()
    root = config_dir.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"invalid cfg file path outside config directory: {rel_path}")
    return candidate


def _macro_version_to_section_text(row: tuple) -> str:
    """Build cfg section text from one stored macro version row."""
    section_type, macro_name, description, rename_existing, gcode, variables_json = row
    header_section = str(section_type or "gcode_macro").strip() or "gcode_macro"
    lines: List[str] = [f"[{header_section} {macro_name}]\n"]

    if description:
        lines.append(f"description: {description}\n")

    if rename_existing:
        lines.append(f"rename_existing: {rename_existing}\n")

    try:
        variables = json.loads(str(variables_json))
    except Exception:
        variables = {}
    if isinstance(variables, dict):
        for key in sorted(variables.keys()):
            lines.append(f"variable_{key}: {variables[key]}\n")

    if gcode:
        lines.append("gcode:\n")
        for line in str(gcode).splitlines():
            # Preserve stored gcode text verbatim to avoid creating
            # whitespace-only diffs during restore/reactivation.
            lines.append(f"{line}\n")

    return "".join(lines)


def macro_row_to_section_text(macro: Dict[str, object]) -> str:
    """Build editable cfg section text from one macro mapping."""
    return _macro_version_to_section_text(
        (
            macro.get("section_type", "gcode_macro"),
            macro.get("macro_name", ""),
            macro.get("description"),
            macro.get("rename_existing"),
            macro.get("gcode"),
            macro.get("variables_json", "{}"),
        )
    )


def _parse_macro_section_text(section_text: str) -> Dict[str, object]:
    """Parse one edited [gcode_macro ...] section from text."""
    normalized_text = str(section_text or "")
    if not normalized_text.strip():
        raise ValueError("macro text is empty")

    lines = normalized_text.splitlines(keepends=True)
    if not lines:
        raise ValueError("macro text is empty")

    header_line = lines[0]
    if not _is_section_header_line(header_line):
        raise ValueError("macro text must start with a [gcode_macro ...] header")

    header = header_line.strip()[1:-1].strip()
    section_type, section_arg = _section_parts(header)
    if section_type != "gcode_macro" or not section_arg:
        raise ValueError("only [gcode_macro ...] sections can be edited")

    description: Optional[str] = None
    rename_existing: Optional[str] = None
    variables: Dict[str, str] = {}
    current_gcode_lines: List[str] = []
    in_gcode_block = False

    for line in lines[1:]:
        if _is_section_header_line(line):
            raise ValueError("macro text must contain exactly one section")

        if in_gcode_block:
            current_gcode_lines.append(line.rstrip("\n"))
            continue

        pair = _parse_key_value(line)
        if not pair:
            continue

        key, value = pair
        if key == "gcode":
            in_gcode_block = True
            if value:
                current_gcode_lines.append(value)
        elif key == "description":
            description = value or None
        elif key == "rename_existing":
            rename_existing = value or None
        elif key.startswith("variable_"):
            variables[key[len("variable_") :]] = value

    while current_gcode_lines and _is_trailing_gcode_comment_or_blank(current_gcode_lines[-1]):
        current_gcode_lines.pop()

    return {
        "section_type": section_type,
        "macro_name": str(section_arg),
        "description": description,
        "rename_existing": rename_existing,
        "gcode": "\n".join(current_gcode_lines).rstrip("\n") if current_gcode_lines else None,
        "variables_json": json.dumps(variables, separators=(",", ":"), sort_keys=True),
        "section_text": normalized_text if normalized_text.endswith("\n") else f"{normalized_text}\n",
    }


def _replace_or_append_macro_section(cfg_file: Path, macro_name: str, section_text: str) -> str:
    """Replace all matching macro sections with one section, or append if missing."""
    try:
        lines = cfg_file.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
    except FileNotFoundError:
        lines = []

    output: List[str] = []
    skipping_target = False
    removed_sections = 0
    insert_index: int | None = None

    for line in lines:
        if _is_section_header_line(line):
            header = line.strip()[1:-1].strip()
            section_type, section_arg = _section_parts(header)
            is_target = section_type == "gcode_macro" and str(section_arg or "") == macro_name
            if is_target:
                if insert_index is None:
                    insert_index = len(output)
                removed_sections += 1
                skipping_target = True
                continue
            skipping_target = False

        if skipping_target:
            continue
        output.append(line)

    normalized_section = section_text if section_text.endswith("\n") else f"{section_text}\n"
    if insert_index is not None:
        output.insert(insert_index, normalized_section)
        operation = "replaced"
    else:
        if output and output[-1].strip():
            output.append("\n")
        output.append(normalized_section)
        operation = "appended"

    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text("".join(output), encoding="utf-8")
    return operation


def restore_macro_version(
    db_path: Path,
    config_dir: Path,
    file_path: str,
    macro_name: str,
    version: int,
) -> Dict[str, object]:
    """Restore one stored macro version into its cfg file content."""
    if version <= 0:
        raise ValueError("version must be a positive integer")

    with open_sqlite_connection(db_path, ensure_schema=ensure_schema) as conn:
        row = conn.execute(
            """
            SELECT section_type, macro_name, description, rename_existing, gcode, variables_json
            FROM macros
            WHERE file_path = ? AND macro_name = ? AND version = ?
            LIMIT 1
            """,
            (file_path, macro_name, int(version)),
        ).fetchone()

    if row is None:
        raise ValueError("macro version not found")

    config_dir = config_dir.expanduser().resolve()
    cfg_file = _safe_cfg_path(config_dir, file_path)
    section_text = _macro_version_to_section_text(row)
    operation = _replace_or_append_macro_section(cfg_file, macro_name, section_text)

    return {
        "file_path": file_path,
        "macro_name": macro_name,
        "version": int(version),
        "operation": operation,
    }


def save_macro_edit(
    config_dir: Path,
    file_path: str,
    macro_name: str,
    section_text: str,
) -> Dict[str, object]:
    """Write an edited macro section back to its cfg file."""
    parsed = _parse_macro_section_text(section_text)
    parsed_macro_name = str(parsed.get("macro_name", ""))
    if parsed_macro_name != macro_name:
        raise ValueError("macro renaming is not supported")

    config_dir = config_dir.expanduser().resolve()
    cfg_file = _safe_cfg_path(config_dir, file_path)
    operation = _replace_or_append_macro_section(cfg_file, macro_name, str(parsed["section_text"]))
    return {
        "file_path": file_path,
        "macro_name": macro_name,
        "operation": operation,
    }


def remove_deleted_macro(db_path: Path, file_path: str, macro_name: str) -> Dict[str, object]:
    """Permanently delete one macro identity from DB when marked as deleted.

    Removes all stored versions for (file_path, macro_name) only if its latest
    version is currently marked is_deleted=1.
    """
    with open_sqlite_connection(db_path, ensure_schema=ensure_schema) as conn:
        latest = conn.execute(
            """
            SELECT version, is_deleted
            FROM macros
            WHERE file_path = ? AND macro_name = ?
            ORDER BY version DESC
            LIMIT 1
            """,
            (file_path, macro_name),
        ).fetchone()

        if latest is None:
            return {
                "removed": 0,
                "reason": "not_found",
            }

        if int(latest[1]) == 0:
            return {
                "removed": 0,
                "reason": "not_deleted",
            }

        removed = conn.execute(
            "DELETE FROM macros WHERE file_path = ? AND macro_name = ?",
            (file_path, macro_name),
        ).rowcount
        conn.commit()

    return {
        "removed": int(removed or 0),
        "reason": "removed",
    }


def remove_all_deleted_macros(db_path: Path) -> Dict[str, object]:
    """Permanently remove all macro identities whose latest version is deleted."""
    with open_sqlite_connection(db_path, ensure_schema=ensure_schema) as conn:
        removed = conn.execute(
            f"""
            DELETE FROM macros
            WHERE (file_path, macro_name) IN (
                SELECT m.file_path, m.macro_name
                FROM macros AS m
                INNER JOIN (
                    {_LATEST_VERSION_SUBQUERY}
                ) AS latest
                    ON m.file_path = latest.file_path
                   AND m.macro_name = latest.macro_name
                   AND m.version = latest.max_version
                WHERE m.is_deleted = 1
            )
            """
        ).rowcount
        conn.commit()

    return {
        "removed": int(removed or 0),
    }


def _remove_macro_sections_from_cfg(cfg_file: Path, macro_name: str) -> int:
    """Remove all [gcode_macro <macro_name>] sections from one cfg file."""
    try:
        lines = cfg_file.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
    except FileNotFoundError:
        return 0

    output: List[str] = []
    skipping_target = False
    removed_sections = 0

    for line in lines:
        if _is_section_header_line(line):
            header = line.strip()[1:-1].strip()
            section_type, section_arg = _section_parts(header)
            is_target = section_type == "gcode_macro" and str(section_arg or "") == macro_name
            if is_target:
                skipping_target = True
                removed_sections += 1
                continue
            skipping_target = False

        if skipping_target:
            continue
        output.append(line)

    if removed_sections > 0:
        cfg_file.write_text("".join(output), encoding="utf-8")
    return removed_sections


def resolve_duplicate_macros(
    config_dir: Path,
    keep_choices: Dict[str, str],
    duplicate_groups: Optional[List[Dict[str, object]]] = None,
) -> Dict[str, object]:
    """Delete non-selected duplicate macro definitions from cfg files.

    keep_choices maps macro_name -> file_path (relative to config_dir) to keep.
    When duplicate_groups is provided, removals are scoped to those entries.
    """
    config_dir = config_dir.expanduser().resolve()
    removed_sections = 0
    touched_files: set[str] = set()

    if duplicate_groups is not None:
        for group in duplicate_groups:
            macro_name = str(group.get("macro_name", ""))
            if not macro_name:
                continue

            keep_file = str(keep_choices.get(macro_name, ""))
            if not keep_file:
                continue

            entries = list(group.get("entries", []))
            for entry in entries:
                rel_path = str(entry.get("file_path", ""))
                if not rel_path or rel_path == keep_file:
                    continue
                cfg_file = _safe_cfg_path(config_dir, rel_path)
                removed_in_file = _remove_macro_sections_from_cfg(cfg_file, macro_name)
                if removed_in_file > 0:
                    removed_sections += removed_in_file
                    touched_files.add(rel_path)
    else:
        for macro_name, keep_file in keep_choices.items():
            group_files = [
                rel
                for rel in (str(p.relative_to(config_dir)) for p in _iter_cfg_files(config_dir))
                if rel != keep_file
            ]
            for rel_path in group_files:
                cfg_file = _safe_cfg_path(config_dir, rel_path)
                removed_in_file = _remove_macro_sections_from_cfg(cfg_file, macro_name)
                if removed_in_file > 0:
                    removed_sections += removed_in_file
                    touched_files.add(rel_path)

    return {
        "removed_sections": removed_sections,
        "touched_files": sorted(touched_files),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    """Create CLI argument parser for standalone indexing runs."""
    parser = argparse.ArgumentParser(
        description="Scan Klipper .cfg files for [gcode_macro ...] sections and store them in SQLite."
    )
    parser.add_argument(
        "--config-dir",
        default="~/printer_data/config",
        help="Path to Klipper config directory (default: ~/printer_data/config)",
    )
    parser.add_argument(
        "--db-path",
        default="~/printer_data/db/klipper_macros.db",
        help="Path to SQLite DB file (default: ~/printer_data/db/klipper_macros.db)",
    )
    return parser


def main() -> int:
    """CLI entrypoint for scanner mode."""
    args = build_arg_parser().parse_args()
    config_dir = Path(args.config_dir).expanduser().resolve()
    db_path = Path(args.db_path).expanduser().resolve()

    try:
        result = run_indexing(config_dir=config_dir, db_path=db_path)
    except FileNotFoundError:
        print(f"ERROR: config directory not found: {config_dir}")
        return 2

    print(f"Scanned cfg files   : {result['cfg_files_scanned']}")
    print(f"New versions stored : {result['macros_inserted']}")
    print(f"Unchanged (skipped) : {result['macros_unchanged']}")
    print(f"Database            : {result['db_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
