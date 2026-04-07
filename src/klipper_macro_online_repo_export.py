#!/usr/bin/env python3
# Copyright (C) 2026 Juergen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build repository-ready online update bundles from local active macros."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import cast
import zipfile

from klipper_macro_indexer import load_macro_list, macro_row_to_section_text


def _normalize_component(value: str) -> str:
    """Normalize vendor/model components for folder paths."""
    normalized = str(value or "").strip().lower()
    if not normalized:
        raise ValueError("vendor/model must not be empty")
    return re.sub(r"[^a-z0-9._-]+", "-", normalized)


def _safe_macro_file_name(macro_name: str) -> str:
    """Return a stable file-safe name for a macro JSON file."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(macro_name or "").strip())
    return f"{cleaned or 'macro'}.json"


def _sha256(text: str) -> str:
    """Compute SHA-256 checksum for UTF-8 text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def export_online_update_repository_zip(
    *,
    db_path: Path,
    out_file: Path,
    source_vendor: str,
    source_model: str,
    now_ts: int | None = None,
) -> dict[str, object]:
    """Export active local macros into a zip for the online update repository."""
    vendor = _normalize_component(source_vendor)
    model = _normalize_component(source_model)
    timestamp = int(now_ts) if now_ts is not None else int(datetime.now(tz=timezone.utc).timestamp())

    macros = load_macro_list(db_path, limit=100000)
    active_macros = [
        macro
        for macro in macros
        if bool(macro.get("is_active", False)) and not bool(macro.get("is_deleted", False))
    ]
    if not active_macros:
        raise ValueError("no active macros available for export")

    macro_entries: list[dict[str, object]] = []
    files_to_write: dict[str, str] = {}

    for macro in active_macros:
        macro_name = str(macro.get("macro_name", "")).strip()
        if not macro_name:
            continue

        section_text = macro_row_to_section_text(macro)
        file_name = _safe_macro_file_name(macro_name)
        relative_path = f"{vendor}/{model}/{file_name}"
        checksum = _sha256(section_text)

        indexed_at_raw = cast(int | None, macro.get("indexed_at", timestamp))
        try:
            indexed_at = int(indexed_at_raw) if indexed_at_raw is not None else timestamp
        except (TypeError, ValueError):
            indexed_at = timestamp
        version = datetime.fromtimestamp(indexed_at, tz=timezone.utc).strftime("%Y-%m-%d")
        macro_payload = {
            "macro_name": macro_name,
            "source_file_path": "macros.cfg",
            "version": version,
            "section_text": section_text,
        }

        files_to_write[relative_path] = json.dumps(macro_payload, indent=2, ensure_ascii=False)
        macro_entries.append(
            {
                "vendor": vendor,
                "model": model,
                "macro_name": macro_name,
                "path": relative_path,
                "version": version,
                "checksum_sha256": checksum,
            }
        )

    if not macro_entries:
        raise ValueError("no valid active macros available for export")

    manifest = {
        "manifest_version": "1",
        "generated_at": timestamp,
        "macros": sorted(
            macro_entries,
            key=lambda entry: (str(entry["macro_name"]).lower(), str(entry["path"]).lower()),
        ),
    }

    out_path = out_file.expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("updates/manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        for rel_path, payload in files_to_write.items():
            archive.writestr(rel_path, payload)

    return {
        "file_path": str(out_path),
        "macro_count": len(macro_entries),
        "source_vendor": vendor,
        "source_model": model,
    }
