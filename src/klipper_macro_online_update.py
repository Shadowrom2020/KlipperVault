#!/usr/bin/env python3
# Copyright (C) 2026 Juergen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Online macro update checks and imports from GitHub manifests."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from klipper_macro_indexer import (
    _gcode_equivalent,
    _make_checksum,
    _parse_macro_section_text,
    ensure_schema,
)
from klipper_vault_db import open_sqlite_connection


def _normalize_identity_component(value: str) -> str:
    """Normalize one identity component for case-insensitive comparisons."""
    return str(value or "").strip().lower()


def _default_online_file_path(source_vendor: str, source_model: str) -> str:
    """Build stable DB identity path for online imports per vendor/model."""
    return "macros.cfg"


def _parse_github_repo(repo_url: str) -> tuple[str, str]:
    """Parse GitHub repository URL and return (owner, repo)."""
    parsed = urlparse(str(repo_url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("online update repository URL must use http/https")
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        raise ValueError("only github.com repositories are supported")

    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        raise ValueError("invalid GitHub repository URL")

    owner = parts[0]
    repo = parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        raise ValueError("invalid GitHub repository URL")
    return owner, repo


def _build_raw_url(repo_url: str, ref: str, file_path: str) -> str:
    """Build raw.githubusercontent.com URL for one repository path."""
    owner, repo = _parse_github_repo(repo_url)
    clean_ref = str(ref or "main").strip() or "main"
    clean_path = str(file_path or "").strip().lstrip("/")
    if not clean_path:
        raise ValueError("remote file path is empty")
    encoded_path = "/".join(quote(part, safe="") for part in clean_path.split("/"))
    encoded_ref = quote(clean_ref, safe="")
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{encoded_ref}/{encoded_path}"


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
    return row


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

    updates: List[Dict[str, object]] = []
    unchanged = 0

    with open_sqlite_connection(
        db_path,
        ensure_schema=ensure_schema,
        pragmas=("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL"),
    ) as conn:
        for entry in manifest_entries:
            remote_payload = _fetch_remote_macro_payload(
                clean_repo,
                clean_ref,
                source_vendor,
                source_model,
                entry,
            )
            parsed = _parse_macro_section_text(str(remote_payload["section_text"]))
            macro_name = str(parsed.get("macro_name", "")).strip()
            remote_checksum = _make_checksum(str(parsed.get("section_text", remote_payload["section_text"])))

            local_latest = _local_latest_source_row(conn, source_vendor, source_model, macro_name)
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
                updates.append(
                    {
                        "identity": (
                            f"{_normalize_identity_component(source_vendor)}::"
                            f"{_normalize_identity_component(source_model)}::{macro_name}"
                        ),
                        "macro_name": macro_name,
                        "source_vendor": _normalize_identity_component(source_vendor),
                        "source_model": _normalize_identity_component(source_model),
                        "section_text": str(parsed.get("section_text", remote_payload["section_text"])),
                        "source_file_path": str(remote_payload.get("source_file_path", "")),
                        "remote_path": str(remote_payload.get("remote_path", "")),
                        "remote_version": str(remote_payload.get("remote_version", "")),
                        "remote_checksum": remote_checksum,
                        "local_version": local_version,
                    }
                )
            else:
                unchanged += 1

        conn.commit()

    return {
        "checked": len(manifest_entries),
        "changed": len(updates),
        "unchanged": unchanged,
        "source_vendor": _normalize_identity_component(source_vendor),
        "source_model": _normalize_identity_component(source_model),
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
        for item in updates:
            source_vendor = _normalize_identity_component(str(item.get("source_vendor", "")))
            source_model = _normalize_identity_component(str(item.get("source_model", "")))
            section_text = str(item.get("section_text", "")).strip()
            if not source_vendor or not source_model or not section_text:
                continue

            parsed = _parse_macro_section_text(section_text)
            macro_name = str(parsed.get("macro_name", "")).strip()
            if not macro_name:
                continue

            file_path = str(item.get("source_file_path", "")).strip() or _default_online_file_path(source_vendor, source_model)
            body_checksum = _make_checksum(str(parsed.get("section_text", section_text)))

            latest = conn.execute(
                """
                SELECT version, body_checksum
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
                    version,
                    indexed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    source_vendor,
                    source_model,
                    "online",
                    str(repo_url or "").strip(),
                    str(repo_ref or "").strip(),
                    str(item.get("remote_path", "")).strip(),
                    str(item.get("remote_version", "")).strip(),
                    new_version,
                    ts,
                ),
            )
            imported_items.append(
                {
                    "identity": str(item.get("identity", "")),
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
