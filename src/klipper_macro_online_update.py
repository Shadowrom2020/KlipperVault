#!/usr/bin/env python3
# Copyright (C) 2026 Juergen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Online macro update checks and imports from GitHub manifests."""

from __future__ import annotations

import json
import concurrent.futures
import functools
import sqlite3
import time
from pathlib import Path
from typing import Callable, Dict, List
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ValidationError, field_validator

from klipper_macro_indexer import (
    _gcode_equivalent,
    _make_checksum,
    _parse_macro_section_text,
    ensure_schema,
)
from klipper_type_utils import to_text as _as_text
from klipper_vault_db import open_sqlite_connection
from klipper_repo_url_utils import build_raw_githubusercontent_url


@functools.lru_cache(maxsize=256)
def _normalize_identity_component(value: str) -> str:
    """Normalize one identity component for case-insensitive comparisons."""
    return str(value or "").strip().lower()


class OnlineImportCandidate(BaseModel):
    """Normalized update item payload used for online import processing."""

    identity: str = ""
    source_vendor: str
    source_model: str
    section_text: str
    source_file_path: str = ""
    remote_path: str = ""
    remote_version: str = ""

    @field_validator("source_vendor", "source_model", mode="before")
    @classmethod
    def normalize_identity(cls, v: object) -> str:
        """Normalize vendor/model to lowercase."""
        normalized = _normalize_identity_component(_as_text(v))
        if not normalized:
            raise ValueError("vendor/model must not be empty")
        return normalized

    @field_validator("section_text", mode="before")
    @classmethod
    def validate_section_text(cls, v: object) -> str:
        """Validate section_text is not empty after normalization."""
        text = _as_text(v)
        if not text:
            raise ValueError("section_text must not be empty")
        return text

    @field_validator("identity", "source_file_path", "remote_path", "remote_version", mode="before")
    @classmethod
    def normalize_optional_text(cls, v: object) -> str:
        """Normalize optional text fields."""
        return _as_text(v)


def _parse_online_import_candidate(item: object) -> OnlineImportCandidate | None:
    """Parse one import item payload and return normalized candidate or None."""
    if not isinstance(item, dict):
        return None

    try:
        return OnlineImportCandidate(**item)
    except ValidationError:
        return None


def _default_online_file_path(source_vendor: str, source_model: str) -> str:
    """Build stable DB identity path for online imports per vendor/model."""
    return "macros.cfg"


def _build_raw_url(repo_url: str, ref: str, file_path: str) -> str:
    """Build raw.githubusercontent.com URL for one repository path."""
    return build_raw_githubusercontent_url(
        repo_url,
        repo_ref=ref,
        file_path=file_path,
        invalid_scheme_error="online update repository URL must use http/https",
        invalid_host_error="only github.com repositories are supported",
        invalid_path_error="invalid GitHub repository URL",
        empty_path_error="remote file path is empty",
    )


def _fetch_json_url(url: str, timeout: float = 10.0) -> object:
    """Fetch and parse a JSON payload from HTTP(S)."""
    request = Request(url, headers={"User-Agent": "KlipperVault/online-updater"})
    try:
        with urlopen(request, timeout=timeout) as response:  # nosec:B310
            payload = response.read().decode("utf-8")
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} while fetching update data") from exc
    except URLError as exc:
        raise RuntimeError(f"network error while fetching update data: {exc.reason}") from exc

    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("remote update file is not valid JSON") from exc


def _manifest_entries_for_identity(
    manifest: object,
    source_vendor: str,
    source_model: str,
) -> List[Dict[str, object]]:
    """Extract update entries for one vendor/model from a manifest payload."""
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a JSON object")

    vendor_norm = _normalize_identity_component(source_vendor)
    model_norm = _normalize_identity_component(source_model)
    if not vendor_norm or not model_norm:
        raise ValueError("printer vendor/model must be configured before checking updates")

    entries: List[Dict[str, object]] = []
    macros = manifest.get("macros", [])
    if isinstance(macros, list):
        for item in macros:
            if not isinstance(item, dict):
                continue
            item_vendor = _normalize_identity_component(str(item.get("vendor", "")))
            item_model = _normalize_identity_component(str(item.get("model", "")))
            if item_vendor == vendor_norm and item_model == model_norm:
                entries.append(item)

    vendors = manifest.get("vendors")
    if isinstance(vendors, dict):
        vendor_block = None
        for key, value in vendors.items():
            if _normalize_identity_component(str(key)) == vendor_norm and isinstance(value, dict):
                vendor_block = value
                break

        if isinstance(vendor_block, dict):
            models = vendor_block.get("models")
            if isinstance(models, dict):
                model_block = None
                for key, value in models.items():
                    if _normalize_identity_component(str(key)) == model_norm and isinstance(value, (dict, list)):
                        model_block = value
                        break
                if isinstance(model_block, list):
                    entries.extend(item for item in model_block if isinstance(item, dict))
                elif isinstance(model_block, dict):
                    model_macros = model_block.get("macros", [])
                    if isinstance(model_macros, list):
                        entries.extend(item for item in model_macros if isinstance(item, dict))

    return entries


def _fetch_remote_macro_payload(
    repo_url: str,
    repo_ref: str,
    source_vendor: str,
    source_model: str,
    entry: Dict[str, object],
) -> Dict[str, object]:
    """Resolve one manifest entry to a macro payload with section text."""
    if "section_text" in entry:
        payload = dict(entry)
    else:
        remote_path = str(entry.get("path", "")).strip().lstrip("/")
        if not remote_path:
            raise ValueError("manifest entry is missing path/section_text")

        expected_prefix = (
            f"{_normalize_identity_component(source_vendor)}/"
            f"{_normalize_identity_component(source_model)}/"
        )
        if not _normalize_identity_component(remote_path).startswith(expected_prefix):
            raise ValueError("manifest path must be under vendor/model folders")

        content = _fetch_json_url(_build_raw_url(repo_url, repo_ref, remote_path))
        if isinstance(content, dict):
            payload = content
        elif isinstance(content, str):
            payload = {"section_text": content}
        else:
            raise ValueError("remote macro file must be a JSON object or section string")
        payload["path"] = remote_path

    section_text = str(payload.get("section_text", "")).strip()
    if not section_text:
        raise ValueError("remote macro payload has no section_text")

    parsed = _parse_macro_section_text(section_text)
    macro_name = str(payload.get("macro_name") or parsed.get("macro_name") or "").strip()
    if not macro_name:
        raise ValueError("remote macro payload has no macro_name")

    return {
        "macro_name": macro_name,
        "section_text": str(parsed.get("section_text", section_text)),
        "source_file_path": str(payload.get("source_file_path", "")).strip()
        or _default_online_file_path(source_vendor, source_model),
        "remote_path": str(payload.get("path") or entry.get("path") or "").strip(),
        "remote_version": str(payload.get("version") or entry.get("version") or "").strip(),
    }


def _bulk_load_local_rows(
    conn,
    source_vendor: str,
    source_model: str,
    macro_names: List[str],
) -> Dict[str, tuple]:
    """Batch load latest local rows for multiple macro names in two queries."""
    if not macro_names:
        return {}
    vendor_norm = _normalize_identity_component(source_vendor)
    model_norm = _normalize_identity_component(source_model)
    placeholders = ",".join("?" * len(macro_names))

    # Primary: match by vendor + model + macro_name (returns section_type … indexed_at)
    rows = conn.execute(
        f"""
        SELECT macro_name, section_type, description, rename_existing, gcode,
               variables_json, body_checksum, file_path, version, indexed_at
        FROM macros
        WHERE lower(source_vendor) = ?
          AND lower(source_model) = ?
          AND macro_name IN ({placeholders})
        ORDER BY indexed_at DESC, version DESC
        """,
        [vendor_norm, model_norm, *macro_names],
    ).fetchall()

    result: Dict[str, tuple] = {}
    for row in rows:
        mn = row[0]
        if mn not in result:
            result[mn] = row[1:]  # drop macro_name; indices 0-8 match _local_latest_source_row

    missing = [n for n in macro_names if n not in result]
    if missing:
        fb_ph = ",".join("?" * len(missing))
        fb_rows = conn.execute(
            f"""
            SELECT macro_name, section_type, description, rename_existing, gcode,
                   variables_json, body_checksum, file_path, version, indexed_at
            FROM macros
            WHERE macro_name IN ({fb_ph})
              AND is_deleted = 0
            ORDER BY is_active DESC, is_loaded DESC, indexed_at DESC, version DESC
            """,
            missing,
        ).fetchall()
        for row in fb_rows:
            mn = row[0]
            if mn not in result:
                result[mn] = row[1:]

    return result


def _local_latest_source_row(conn, source_vendor: str, source_model: str, macro_name: str):
    """Fetch latest stored row matching source vendor/model/macro identity."""
    vendor_norm = _normalize_identity_component(source_vendor)
    model_norm = _normalize_identity_component(source_model)
    row = conn.execute(
        """
        SELECT
            section_type,
            description,
            rename_existing,
            gcode,
            variables_json,
            body_checksum,
            file_path,
            version,
            indexed_at
        FROM macros
        WHERE lower(source_vendor) = ?
          AND lower(source_model) = ?
          AND macro_name = ?
        ORDER BY indexed_at DESC, version DESC
        LIMIT 1
        """,
        (vendor_norm, model_norm, macro_name),
    ).fetchone()
    if row is not None:
        return row

    # Fallback for pre-import local macros that do not yet carry source identity
    # metadata. Prefer the active/latest non-deleted row for overwrite targeting.
    return conn.execute(
        """
        SELECT
            section_type,
            description,
            rename_existing,
            gcode,
            variables_json,
            body_checksum,
            file_path,
            version,
            indexed_at
        FROM macros
        WHERE macro_name = ?
          AND is_deleted = 0
        ORDER BY is_active DESC, is_loaded DESC, indexed_at DESC, version DESC
        LIMIT 1
        """,
        (macro_name,),
    ).fetchone()


def _upsert_online_state(
    conn,
    *,
    source_vendor: str,
    source_model: str,
    macro_name: str,
    remote_repo_url: str,
    remote_ref: str,
    remote_path: str,
    remote_version: str,
    remote_checksum: str,
    update_available: bool,
    checked_at: int,
) -> None:
    """Persist last check details for one remote macro identity."""
    conn.execute(
        """
        INSERT INTO macro_online_update_state (
            source_vendor,
            source_model,
            macro_name,
            remote_repo_url,
            remote_ref,
            remote_path,
            remote_version,
            remote_checksum,
            update_available,
            last_checked
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_vendor, source_model, macro_name, remote_repo_url, remote_path)
        DO UPDATE SET
            remote_ref = excluded.remote_ref,
            remote_version = excluded.remote_version,
            remote_checksum = excluded.remote_checksum,
            update_available = excluded.update_available,
            last_checked = excluded.last_checked
        """,
        (
            _normalize_identity_component(source_vendor),
            _normalize_identity_component(source_model),
            macro_name,
            remote_repo_url,
            remote_ref,
            remote_path,
            remote_version,
            remote_checksum,
            1 if update_available else 0,
            int(checked_at),
        ),
    )


def check_online_macro_updates(
    db_path: Path,
    *,
    repo_url: str,
    manifest_path: str,
    repo_ref: str,
    source_vendor: str,
    source_model: str,
    now_ts: int | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> Dict[str, object]:
    """Return changed remote macros for configured source vendor/model."""
    clean_repo = str(repo_url or "").strip()
    if not clean_repo:
        raise ValueError("online update repository URL is not configured")

    clean_manifest = str(manifest_path or "updates/manifest.json").strip() or "updates/manifest.json"
    clean_ref = str(repo_ref or "main").strip() or "main"
    checked_at = int(now_ts) if now_ts is not None else int(time.time())

    manifest = _fetch_json_url(_build_raw_url(clean_repo, clean_ref, clean_manifest))
    manifest_entries = _manifest_entries_for_identity(manifest, source_vendor, source_model)
    total = len(manifest_entries)
    if progress_callback is not None:
        progress_callback(0, total)

    source_vendor_norm = _normalize_identity_component(source_vendor)
    source_model_norm = _normalize_identity_component(source_model)
    updates: List[Dict[str, object]] = []
    unchanged = 0

    # ---- Phase 1: fetch all remote payloads in parallel ----
    def _fetch_one(args: tuple) -> Dict[str, object]:
        _idx, entry = args
        return _fetch_remote_macro_payload(clean_repo, clean_ref, source_vendor, source_model, entry)

    max_workers = min(8, max(1, total))
    fetch_results: List[Dict[str, object]] = [None] * total  # type: ignore[list-item]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_idx = {
            executor.submit(_fetch_one, (i, entry)): i
            for i, entry in enumerate(manifest_entries)
        }
        for future in concurrent.futures.as_completed(future_to_idx):
            idx = future_to_idx[future]
            fetch_results[idx] = future.result()  # raises on error, propagated to caller

    # ---- Phase 2: parse payloads and bulk-load local rows ----
    parsed_remotes: List[tuple] = []
    for remote_payload in fetch_results:
        parsed = _parse_macro_section_text(str(remote_payload["section_text"]))
        macro_name = str(parsed.get("macro_name", "")).strip()
        remote_checksum = _make_checksum(str(parsed.get("section_text", remote_payload["section_text"])))
        parsed_remotes.append((remote_payload, parsed, macro_name, remote_checksum))

    macro_names = [x[2] for x in parsed_remotes]

    with open_sqlite_connection(
        db_path,
        ensure_schema=ensure_schema,
        pragmas=("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL"),
    ) as conn:
        local_rows = _bulk_load_local_rows(conn, source_vendor, source_model, macro_names)

        for index, (remote_payload, parsed, macro_name, remote_checksum) in enumerate(
            parsed_remotes, start=1
        ):
            local_latest = local_rows.get(macro_name)
            changed = True
            local_version = 0
            if local_latest is not None:
                local_version = int(local_latest[7])
                parsed_gcode_raw = parsed.get("gcode")
                parsed_gcode = str(parsed_gcode_raw) if parsed_gcode_raw is not None else None
                changed = not (
                    str(local_latest[0] or "gcode_macro") == str(parsed.get("section_type", "gcode_macro"))
                    and local_latest[1] == parsed.get("description")
                    and local_latest[2] == parsed.get("rename_existing")
                    and _gcode_equivalent(local_latest[3], parsed_gcode)
                    and str(local_latest[4]) == str(parsed.get("variables_json", "{}"))
                )

            _upsert_online_state(
                conn,
                source_vendor=source_vendor,
                source_model=source_model,
                macro_name=macro_name,
                remote_repo_url=clean_repo,
                remote_ref=clean_ref,
                remote_path=str(remote_payload.get("remote_path", "")),
                remote_version=str(remote_payload.get("remote_version", "")),
                remote_checksum=remote_checksum,
                update_available=changed,
                checked_at=checked_at,
            )

            if changed:
                target_file_path = str(remote_payload.get("source_file_path", ""))
                if local_latest is not None:
                    target_file_path = str(local_latest[6] or "").strip() or target_file_path
                updates.append(
                    {
                        "identity": (
                            f"{source_vendor_norm}::"
                            f"{source_model_norm}::{macro_name}"
                        ),
                        "macro_name": macro_name,
                        "source_vendor": source_vendor_norm,
                        "source_model": source_model_norm,
                        "section_text": str(parsed.get("section_text", remote_payload["section_text"])),
                        "source_file_path": target_file_path,
                        "remote_path": str(remote_payload.get("remote_path", "")),
                        "remote_version": str(remote_payload.get("remote_version", "")),
                        "remote_checksum": remote_checksum,
                        "local_version": local_version,
                    }
                )
            else:
                unchanged += 1

            if progress_callback is not None:
                progress_callback(index, total)

        conn.commit()

    return {
        "checked": len(manifest_entries),
        "changed": len(updates),
        "unchanged": unchanged,
        "source_vendor": source_vendor_norm,
        "source_model": source_model_norm,
        "repo_url": clean_repo,
        "repo_ref": clean_ref,
        "manifest_path": clean_manifest,
        "updates": updates,
    }


def import_online_macro_updates(
    db_path: Path,
    *,
    updates: List[Dict[str, object]],
    repo_url: str,
    repo_ref: str,
    printer_profile_id: int | None = None,
    now_ts: int | None = None,
) -> Dict[str, object]:
    """Insert changed remote macros as new inactive versions."""
    if not updates:
        return {"imported": 0, "imported_items": []}

    imported_items: List[Dict[str, object]] = []
    ts = int(now_ts) if now_ts is not None else int(time.time())

    with open_sqlite_connection(
        db_path,
        ensure_schema=ensure_schema,
        pragmas=("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL"),
    ) as conn:
        fallback_printer_profile_id: int | None = int(printer_profile_id) if printer_profile_id is not None else None
        if fallback_printer_profile_id is None:
            row = conn.execute(
                """
                SELECT printer_profile_id
                FROM macros
                WHERE printer_profile_id IS NOT NULL
                ORDER BY indexed_at DESC, version DESC
                LIMIT 1
                """
            ).fetchone()
            if row is not None and row[0] is not None:
                fallback_printer_profile_id = int(row[0])

        if fallback_printer_profile_id is None:
            try:
                row = conn.execute(
                """
                SELECT id
                FROM printer_profiles
                WHERE is_active = 1
                ORDER BY id DESC
                LIMIT 1
                """
                ).fetchone()
                if row is None:
                    row = conn.execute(
                        """
                        SELECT id
                        FROM printer_profiles
                        ORDER BY id ASC
                        LIMIT 1
                        """
                    ).fetchone()
                if row is not None:
                    fallback_printer_profile_id = int(row[0])
            except sqlite3.OperationalError:
                pass

        for item in updates:
            candidate = _parse_online_import_candidate(item)
            if candidate is None:
                continue

            parsed = _parse_macro_section_text(candidate.section_text)
            macro_name = str(parsed.get("macro_name", "")).strip()
            if not macro_name:
                continue

            file_path = candidate.source_file_path or _default_online_file_path(
                candidate.source_vendor,
                candidate.source_model,
            )
            local_latest = _local_latest_source_row(
                conn,
                candidate.source_vendor,
                candidate.source_model,
                macro_name,
            )
            if local_latest is not None:
                resolved_file_path = str(local_latest[6] or "").strip()
                if resolved_file_path:
                    file_path = resolved_file_path
            body_checksum = _make_checksum(str(parsed.get("section_text", candidate.section_text)))

            latest = conn.execute(
                """
                    SELECT version, body_checksum, printer_profile_id
                FROM macros
                WHERE file_path = ? AND macro_name = ?
                ORDER BY version DESC
                LIMIT 1
                """,
                (file_path, macro_name),
            ).fetchone()
            latest_version = int(latest[0]) if latest is not None else 0
            if latest is not None and str(latest[1]) == body_checksum:
                continue
            row_printer_profile_id = fallback_printer_profile_id
            if latest is not None and latest[2] is not None:
                row_printer_profile_id = int(latest[2])
            if row_printer_profile_id is None:
                # Last-resort fallback keeps inserts valid even in unusual bootstrap states.
                row_printer_profile_id = 1

            new_version = latest_version + 1
            conn.execute(
                """
                INSERT INTO macros (
                    file_path,
                    section_type,
                    macro_name,
                    line_number,
                    description,
                    rename_existing,
                    gcode,
                    variables_json,
                    body_checksum,
                    is_active,
                    runtime_macro_name,
                    renamed_from,
                    is_deleted,
                    is_loaded,
                    is_dynamic,
                    is_new,
                    source_vendor,
                    source_model,
                    import_source,
                    remote_repo_url,
                    remote_ref,
                    remote_path,
                    remote_version,
                    printer_profile_id,
                    version,
                    indexed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_path,
                    str(parsed.get("section_type", "gcode_macro")),
                    macro_name,
                    1,
                    parsed.get("description"),
                    parsed.get("rename_existing"),
                    parsed.get("gcode"),
                    str(parsed.get("variables_json", "{}")),
                    body_checksum,
                    0,
                    macro_name,
                    None,
                    0,
                    0,
                    0,
                    1,
                    candidate.source_vendor,
                    candidate.source_model,
                    "online",
                    _as_text(repo_url),
                    _as_text(repo_ref),
                    candidate.remote_path,
                    candidate.remote_version,
                    row_printer_profile_id,
                    new_version,
                    ts,
                ),
            )
            imported_items.append(
                {
                    "identity": candidate.identity,
                    "file_path": file_path,
                    "macro_name": macro_name,
                    "version": new_version,
                }
            )

        conn.commit()

    return {
        "imported": len(imported_items),
        "imported_items": imported_items,
    }
